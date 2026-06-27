"""
WebSocket Gateway Server

Typed Gateway Protocol v1:
  Client -> Server: chat.send / control.run / chat.inject / chat.abort / chat.history /
                    presence.ping / tools.approval
  Server -> Client: connected / chat.done / chat.token / chat.history /
                    system.event / health.tick / cron.update /
                    tools.approval_request / control.approval_request
"""

# ruff: noqa: E402

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
import uuid
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.genai import types
from pathlib import Path
from pydantic import ValidationError

from src.config.settings import get_settings
from src.agents.root_agent import root_agent
from src.agents.sub_agents import SUB_AGENTS
from src.control_loop.live_failure_taxonomy import classify_control_loop_failure
from src.control_loop.root_workflow import ControlLoop, ExecutionResult
from src.gateway.routing_agent import routing_agent
from src.memory_lifecycle.adk_memory_service import get_promoted_memory_service
from src.security.audit import get_audit_logger, AuditEventType
from src.security.tool_policy import APPROVAL_EXPIRY_REASONS, get_tool_policy_engine
from src.tools.finance import is_direct_stock_price_query, stock_price
from src.tools.web_search import web_search
from src.skills.runtime import ensure_skills_loaded, get_skills_report
from src.tools.skills import (
    capability_invoke as tool_capability_invoke,
    capability_list as tool_capability_list,
    resource_list as tool_resource_list,
    resource_read as tool_resource_read,
    skill_execute as tool_skill_execute,
    skill_list as tool_skill_list,
)
from src.tools.memory import memory_search, memory_stats, memory_delete
from src.tools.subagents import get_subagent_manager, set_subagent_notifier
from src.gateway.protocol import (
    EVENT_SCHEMAS,
    HTTP_ROUTE_SCHEMAS,
    PROTOCOL_VERSION,
    RUNTIME_SUBSTRATE_SCHEMA,
    ev_chat_done, ev_system_event,
    ev_health_tick, ev_cron_update, ev_tools_approval_request,
    ev_control_approval_request,
    ev_tools_approval_update, ev_task_update, ev_audit_append,
    ev_tool_start, ev_tool_result,
)
from src.gateway.routing import (
    RoutingDecision,
    decision_from_payload,
    heuristic_decision,
    targets_user_browser,
)
from src.gateway.task_replay import (
    persist_control_loop_step_events,
)
from src.gateway.control_supervisor import ControlLoopSupervisor
from src.gateway.live_runtime_boundary import (
    GATEWAY_OBSERVATION_PROCESS_PROBE_KIND,
    GATEWAY_ROUTE_INVOCATION_BOUNDARY_PATH,
    GATEWAY_RECOVERY_DECISION_PROCESS_PROBE_KIND,
    GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_PATH,
    _sign_gateway_live_process_probe_evidence,
    build_gateway_route_invocation_boundary,
    build_gateway_supervisor_process_probe_boundary_from_route,
)
from src.gateway.missionos_milestone import (
    build_current_missionos_milestone_summary,
)
from src.gateway.missionos_causal_timeline import (
    build_missionos_causal_timeline_summary,
)
from src.gateway.missionos_envelope_browser import (
    build_missionos_envelope_browser_summary,
)
from src.gateway.missionos_knowledge_browser import (
    build_missionos_knowledge_browser_summary,
)
from src.gateway.missionos_agent_dashboard import (
    build_missionos_agent_dashboard_summary,
)
from src.gateway.missionos_capabilities import (
    MISSIONOS_INTERNAL_CAPABILITIES,
    MISSIONOS_OPERATOR_FACING_ROUTE,
    build_missionos_capability_registry_summary,
    capability_invocation_context,
)
from src.intelligence.llm_dialogue_router import run_llm_dialogue_router
from src.intelligence.missionos_agent_runtime import (
    guard_runtime_recovery_planner_result,
    plan_runtime_recovery_maneuver,
    run_missionos_agent_runtime,
    run_missionos_runtime_recovery_agent,
)
from src.intelligence.missionos_chief_planner_tools import (
    enrich_coordinate_route_with_terrain_profile,
    extract_operator_requested_route_overrides,
    resolve_chief_planner_internal_tools,
)
from src.gateway.missionos_knowledge_sharing import (
    FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV,
    build_form2a_action_consumption_summary,
    build_form2a_operator_review_summary,
    build_form2a_response_selection_summary,
    build_llm_repair_planner_summary,
    build_policy_authority_summary,
    build_scoped_form3_closed_loop_summary,
    build_sitl_dispatch_execution_summary,
    build_missionos_knowledge_sharing_summary,
    run_form2a_action_consumption,
    run_form2a_operator_review_approve,
    run_form2a_operator_review_reject,
    run_form2a_operator_review_request_revision,
    run_form2a_response_selection_from_form1,
    run_llm_repair_planner_from_evidence_payload,
    run_llm_repair_planner_from_latest_evidence,
    run_policy_authority_promotion,
    run_scoped_form3_closed_loop,
    run_sitl_bounded_dispatch_execution,
    run_knowledge_curator_dry_run,
    run_knowledge_curator_production_publish,
)
from src.gateway.missionos_operations import (
    get_missionos_operation_last,
    get_missionos_operation_run,
    get_missionos_operation_run_artifact,
    get_missionos_operations_registry,
    run_missionos_operation,
)
from src.gateway.missionos_milestone import ARTIFACT_ROOT, _relative
from src.gateway.missionos_real_hardware_dispatch import (
    run_real_hardware_arm_disarm_dispatch,
)
from src.runtime.px4_real_hardware_actuator_backend import (
    PX4RealHardwareActuatorError,
    build_px4_real_hardware_actuator_approval,
)
from src.runtime.missionos_payload_split_plan import (
    MISSIONOS_PAYLOAD_SPLIT_DEFAULT_RESERVE_FRACTION,
    MISSIONOS_PAYLOAD_SPLIT_TOOL_NAME,
    apply_payload_split_plan_to_coordinate_route,
    build_missionos_payload_split_plan,
    requested_payload_weight_from_route,
)

MISSIONOS_AUTONOMY_CONVERSATION_AGENT_TIMEOUT_ENV = (
    "MISSIONOS_AUTONOMY_CONVERSATION_AGENT_TIMEOUT_SECONDS"
)
MISSIONOS_AUTONOMY_CONVERSATION_AGENT_TIMEOUT_SECONDS = 12


def _missionos_instruction_text(payload: Mapping[str, Any]) -> str:
    value = payload.get("operator_instruction")
    if isinstance(value, Mapping):
        value = value.get("text") or value.get("instruction") or ""
    return str(value or payload.get("instruction") or payload.get("message") or "").strip()


def _missionos_client_surface(payload: Mapping[str, Any]) -> str:
    surface = str(payload.get("missionos_client_surface") or "").strip().lower()
    return surface if surface in {"chat", "command"} else "command"


def _missionos_conversation_agent_timeout_seconds() -> int:
    value = os.environ.get(MISSIONOS_AUTONOMY_CONVERSATION_AGENT_TIMEOUT_ENV)
    try:
        parsed = int(value) if value is not None else (
            MISSIONOS_AUTONOMY_CONVERSATION_AGENT_TIMEOUT_SECONDS
        )
    except ValueError:
        return MISSIONOS_AUTONOMY_CONVERSATION_AGENT_TIMEOUT_SECONDS
    return max(1, parsed)


_MISSIONOS_MISSION_DESIGNER_CONTEXTS: dict[str, dict[str, Any]] = {}
_MISSIONOS_MISSION_DESIGNER_CONTEXT_LIMIT = 64


def _missionos_request_session_id(payload: Mapping[str, Any]) -> str:
    return str(payload.get("session_id") or "").strip()


def _missionos_context_without_registry_fields(context: Mapping[str, Any]) -> dict[str, Any]:
    stripped = {
        key: value
        for key, value in dict(context).items()
        if key
        not in {
            "mission_designer_context_ref",
            "mission_designer_context_sha256",
            "mission_designer_context_session_id",
        }
    }
    summary = stripped.get("summary")
    if isinstance(summary, Mapping):
        stripped["summary"] = {
            key: value
            for key, value in dict(summary).items()
            if key
            not in {
                "mission_designer_context_ref",
                "mission_designer_context_sha256",
                "mission_designer_context_session_id",
            }
        }
    return stripped


def _missionos_context_sha256(context: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        _missionos_context_without_registry_fields(context),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _missionos_context_ref(context: Mapping[str, Any], sha256: str) -> str:
    proposal = context.get("scenario_proposal")
    proposal_id = ""
    if isinstance(proposal, Mapping):
        proposal_id = str(proposal.get("proposal_id") or "").strip()
    return f"mission_designer_context:{proposal_id or sha256[:16]}"


def _missionos_register_mission_designer_context(
    context: Mapping[str, Any],
    *,
    session_id: str = "",
) -> dict[str, Any]:
    base = _missionos_context_without_registry_fields(context)
    sha256 = _missionos_context_sha256(base)
    ref = _missionos_context_ref(base, sha256)
    summary = dict(base.get("summary")) if isinstance(base.get("summary"), Mapping) else {}
    summary.update(
        {
            "mission_designer_context_ref": ref,
            "mission_designer_context_sha256": sha256,
            "mission_designer_context_session_id": session_id,
        }
    )
    registered = {
        **base,
        "summary": summary,
        "mission_designer_context_ref": ref,
        "mission_designer_context_sha256": sha256,
        "mission_designer_context_session_id": session_id,
    }
    _MISSIONOS_MISSION_DESIGNER_CONTEXTS[ref] = registered
    while len(_MISSIONOS_MISSION_DESIGNER_CONTEXTS) > _MISSIONOS_MISSION_DESIGNER_CONTEXT_LIMIT:
        _MISSIONOS_MISSION_DESIGNER_CONTEXTS.pop(next(iter(_MISSIONOS_MISSION_DESIGNER_CONTEXTS)))
    return dict(registered)


def _missionos_mission_designer_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    context = payload.get("mission_designer_context")
    if not isinstance(context, Mapping):
        return {}
    summary = context.get("summary") if isinstance(context.get("summary"), Mapping) else {}
    ref = str(context.get("mission_designer_context_ref") or summary.get("mission_designer_context_ref") or "").strip()
    sha256 = str(
        context.get("mission_designer_context_sha256")
        or summary.get("mission_designer_context_sha256")
        or ""
    ).strip()
    context_session_id = str(
        context.get("mission_designer_context_session_id")
        or summary.get("mission_designer_context_session_id")
        or ""
    ).strip()
    request_session_id = _missionos_request_session_id(payload)
    if ref and sha256:
        registered = _MISSIONOS_MISSION_DESIGNER_CONTEXTS.get(ref)
        registered_session_id = str(registered.get("mission_designer_context_session_id") or "") if registered else ""
        session_matches = (
            registered_session_id == request_session_id == context_session_id
            if registered_session_id
            else True
        )
        if registered and registered.get("mission_designer_context_sha256") == sha256 and session_matches:
            return dict(registered)
        return {
            "mission_designer_context_error": "mission_designer_context_ref_or_sha256_not_source_bound",
            "mission_designer_context_ref": ref,
            "mission_designer_context_sha256": sha256,
            "mission_designer_context_session_id": context_session_id,
            "summary": {
                "mission_designer_context_status": "rejected",
                "mission_designer_context_error": "mission_designer_context_ref_or_sha256_not_source_bound",
            },
        }
    if any(key in context for key in ("scenario_proposal", "scenario_approval", "bounded_simulation_request")):
        return {
            "mission_designer_context_error": "mission_designer_context_missing_server_ref",
            "summary": {
                "mission_designer_context_status": "rejected",
                "mission_designer_context_error": "mission_designer_context_missing_server_ref",
            },
        }
    return {}


def _missionos_mission_designer_has_proposal(context: Mapping[str, Any]) -> bool:
    proposal = context.get("scenario_proposal")
    validation = context.get("validation_result")
    return isinstance(proposal, Mapping) and isinstance(validation, Mapping)


def _missionos_mission_designer_context_error(context: Mapping[str, Any]) -> str:
    return str(context.get("mission_designer_context_error") or "").strip()


def _missionos_merge_mission_designer_context(
    context: Mapping[str, Any],
    update: Mapping[str, Any],
) -> dict[str, Any]:
    merged = {**dict(context), **dict(update)}
    context_summary = context.get("summary") if isinstance(context.get("summary"), Mapping) else {}
    update_summary = update.get("summary") if isinstance(update.get("summary"), Mapping) else {}
    summary = {**context_summary, **update_summary}
    if summary:
        merged["summary"] = summary
    return merged


def _missionos_payload_split_required(coordinate_route: Mapping[str, Any]) -> bool:
    requested_payload_weight_kg = requested_payload_weight_from_route(coordinate_route)
    if requested_payload_weight_kg is None:
        return False
    planning_limit = MISSION_DESIGNER_CONTRACT_MAX_PAYLOAD_KG * (
        1.0 - MISSIONOS_PAYLOAD_SPLIT_DEFAULT_RESERVE_FRACTION
    )
    return requested_payload_weight_kg > planning_limit


def _missionos_payload_split_plan_for_route(
    coordinate_route: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(coordinate_route, Mapping):
        return {}
    if str(coordinate_route.get("payload_weight_source") or "") == "missionos_payload_split_plan":
        return {}
    if not _missionos_payload_split_required(coordinate_route):
        return {}
    return build_missionos_payload_split_plan(
        coordinate_route=coordinate_route,
        now=datetime.now(timezone.utc),
    )


def _missionos_attach_payload_split_plan_to_tools(
    chief_planner_tools: Mapping[str, Any],
    *,
    coordinate_route: Mapping[str, Any],
    payload_split_plan: Mapping[str, Any],
) -> dict[str, Any]:
    existing_tool_names = list(chief_planner_tools.get("internal_tool_names") or [])
    if MISSIONOS_PAYLOAD_SPLIT_TOOL_NAME not in existing_tool_names:
        existing_tool_names.append(MISSIONOS_PAYLOAD_SPLIT_TOOL_NAME)
    return {
        **dict(chief_planner_tools),
        "schema_version": "missionos_chief_planner_internal_tools.v1",
        "tool_status": chief_planner_tools.get("tool_status") or "resolved",
        "operator_facing_agent": "missionos_chief_agent",
        "subagents_operator_facing": False,
        "internal_tool_names": existing_tool_names,
        "payload_split_plan": dict(payload_split_plan),
        "coordinate_route": dict(coordinate_route),
        "dispatch_authority_created": False,
        "progress_counted": False,
        "resolved_at": chief_planner_tools.get("resolved_at")
        or datetime.now(timezone.utc).isoformat(),
    }


def _missionos_attach_payload_split_plan_to_result(
    designer_result: Mapping[str, Any],
    payload_split_plan: Mapping[str, Any],
) -> dict[str, Any]:
    if not payload_split_plan:
        return dict(designer_result)
    summary = (
        dict(designer_result.get("summary"))
        if isinstance(designer_result.get("summary"), Mapping)
        else {}
    )
    summary.update(
        {
            "payload_split_plan_status": payload_split_plan.get("plan_status"),
            "payload_split_required": payload_split_plan.get("payload_split_required"),
            "requested_total_payload_weight_kg": payload_split_plan.get(
                "requested_payload_weight_kg"
            ),
            "payload_split_sortie_count": payload_split_plan.get("sortie_count"),
            "payload_split_planning_max_payload_weight_kg_per_drone": (
                payload_split_plan.get("planning_max_payload_weight_kg_per_drone")
            ),
            "payload_split_memory_used_for_planning_only": True,
            "payload_split_dispatch_authority_created": False,
        }
    )
    return {
        **dict(designer_result),
        "missionos_payload_split_plan": dict(payload_split_plan),
        "summary": summary,
    }


def _missionos_payload_split_message(payload_split_plan: Mapping[str, Any]) -> str:
    if payload_split_plan.get("plan_status") != "split_required":
        return ""
    requested = payload_split_plan.get("requested_payload_weight_kg")
    planning_limit = payload_split_plan.get("planning_max_payload_weight_kg_per_drone")
    sortie_count = payload_split_plan.get("sortie_count")
    sorties = payload_split_plan.get("sorties")
    payload_values = [
        sortie.get("payload_weight_kg")
        for sortie in (sorties if isinstance(sorties, list) else [])
        if isinstance(sortie, Mapping)
    ]
    if payload_values:
        min_payload = min(payload_values)
        max_payload = max(payload_values)
        per_sortie = (
            f"{max_payload}kg"
            if min_payload == max_payload
            else f"{min_payload}-{max_payload}kg"
        )
    else:
        per_sortie = "bounded payload"
    history = payload_split_plan.get("historical_evidence")
    sample_count = (
        history.get("task_sample_count") if isinstance(history, Mapping) else 0
    )
    return (
        "\nNote: requested payload="
        f"{requested}kg exceeds the per-drone planning max of {planning_limit}kg, "
        f"so MissionOS split it into {sortie_count} sorties at {per_sortie} each. "
        f"It referenced {sample_count} historical task evidence records, but this is "
        "planning evidence only: no approval, dispatch, PX4 upload, or delivery "
        "completion occurred."
    )


def _missionos_prepare_mission_designer_sitl_context(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    proposal = context.get("scenario_proposal")
    validation = context.get("validation_result")
    approval = context.get("scenario_approval")
    compile_result = context.get("scenario_compile_result")
    bounded_request = context.get("bounded_simulation_request")
    required = {
        "scenario_proposal": proposal,
        "validation_result": validation,
        "scenario_approval": approval,
        "scenario_compile_result": compile_result,
        "bounded_simulation_request": bounded_request,
    }
    missing = [key for key, value in required.items() if not isinstance(value, Mapping)]
    if missing:
        raise PX4GazeboMissionScenarioDesignerError(
            "Mission Designer SITL preparation requires approved scenario context: "
            + ", ".join(missing)
        )
    execution_request = build_px4_gazebo_mission_designer_sitl_execution_request(
        proposal=proposal,
        validation=validation,
        approval=approval,
        compile_result=compile_result,
        bounded_simulation_request=bounded_request,
        now=datetime.now(timezone.utc),
    )
    execution_request_payload = execution_request.model_dump(mode="json")
    artifacts: dict[str, Any] = {
        "px4_gazebo_mission_scenario_proposal": dict(proposal),
        "px4_gazebo_mission_scenario_validation_result": dict(validation),
        "px4_gazebo_mission_scenario_approval": dict(approval),
        "px4_gazebo_mission_scenario_compile_result": dict(compile_result),
        "px4_gazebo_bounded_simulation_request": dict(bounded_request),
        "px4_gazebo_mission_designer_sitl_execution_request": execution_request_payload,
    }
    optional_artifacts = {
        "mission_designer_coordinate_pair_route": context.get("mission_designer_coordinate_pair_route"),
        "real_world_target_resolution": context.get("real_world_target_resolution"),
        "terrain_dem_source_snapshot": context.get("terrain_dem_source_snapshot"),
        "terrain_heightmap_file_artifact": context.get("terrain_heightmap_file_artifact"),
        "execution_terrain_fallback_reason": context.get("execution_terrain_fallback_reason"),
        "execution_terrain_source_backed": context.get("execution_terrain_source_backed"),
        "gazebo_world_artifact": context.get("gazebo_world_artifact"),
        "coordinate_transform_candidate": context.get("coordinate_transform_candidate"),
        "digital_twin_sitl_binding_gate": context.get("digital_twin_sitl_binding_gate"),
        "digital_twin_route_plan": context.get("digital_twin_route_plan"),
        "digital_twin_px4_mission_item_candidate": context.get("digital_twin_px4_mission_item_candidate"),
        "mission_scenario_designer_summary": context.get("summary"),
    }
    artifacts.update({key: value for key, value in optional_artifacts.items() if isinstance(value, Mapping)})
    artifacts = enrich_terrain_heightmap_preview_fields(artifacts)
    coordinate_route_artifact = artifacts.get("mission_designer_coordinate_pair_route")
    if isinstance(coordinate_route_artifact, Mapping):
        artifacts["mission_designer_coordinate_pair_sitl_binding"] = (
            _mission_designer_coordinate_pair_sitl_binding(
                route=coordinate_route_artifact,
                scenario_approval=approval,
                bounded_request=bounded_request,
            )
        )
    operator_route_requested = _mission_designer_operator_route_requested(
        artifacts=artifacts,
        metadata={},
    )
    route_bound_to_sitl = _mission_designer_route_bound_to_sitl(artifacts)
    task = get_task_store().create(
        kind="px4_gazebo_mission_designer_sitl_execution_request",
        title="PX4/Gazebo Mission Designer SITL execution request",
        status="pending",
        artifacts=artifacts,
        metadata={
            "source": "missionos_autonomy_conversation_execute",
            "execution_request_id": execution_request.execution_request_id,
            "request_status": execution_request.request_status,
            "execution_mode": execution_request.execution_mode,
            "preparation_scope": execution_request.preparation_scope,
            "requires_explicit_execution_approval": execution_request.requires_explicit_execution_approval,
            "execution_invoked": execution_request.execution_invoked,
            "gazebo_execution_invoked": execution_request.gazebo_execution_invoked,
            "external_dispatch_performed": execution_request.external_dispatch_performed,
            "mavlink_dispatch_performed": execution_request.mavlink_dispatch_performed,
            "px4_mission_upload_performed": execution_request.px4_mission_upload_performed,
            "hardware_target_allowed": execution_request.hardware_target_allowed,
            "physical_execution_invoked": execution_request.physical_execution_invoked,
            "operator_route_requested": operator_route_requested,
            "operator_route_bound_to_sitl": route_bound_to_sitl,
            "operator_route_blocked_reason": (
                _MISSION_DESIGNER_COORDINATE_ROUTE_BLOCKED_REASON
                if operator_route_requested and not route_bound_to_sitl
                else ""
            ),
        },
    )
    return _missionos_register_mission_designer_context(
        _missionos_merge_mission_designer_context(
            context,
            {
                "sitl_execution_request": execution_request_payload,
                "mission_designer_coordinate_pair_sitl_binding": artifacts.get(
                    "mission_designer_coordinate_pair_sitl_binding"
                ),
                "sitl_execution_task": task,
                "summary": {
                    "sitl_execution_request_status": execution_request.request_status,
                    "sitl_execution_task_id": task["task_id"],
                    "task_status": task["status"],
                    "execution_invoked": execution_request.execution_invoked,
                    "gazebo_execution_invoked": execution_request.gazebo_execution_invoked,
                    "mavlink_dispatch_performed": execution_request.mavlink_dispatch_performed,
                    "px4_mission_upload_performed": execution_request.px4_mission_upload_performed,
                    "hardware_target_allowed": execution_request.hardware_target_allowed,
                    "physical_execution_invoked": execution_request.physical_execution_invoked,
                    "operator_route_requested": operator_route_requested,
                    "operator_route_bound_to_sitl": route_bound_to_sitl,
                },
            },
        ),
        session_id=str(context.get("mission_designer_context_session_id") or ""),
    )


def _missionos_instruction_requests_designer_plan(text: str) -> bool:
    lower = text.lower()
    question_tokens = (
        "飛ばせない",
        "飛べない",
        "できない",
        "できますか",
        "できる？",
        "can you",
        "can't",
        "cannot",
    )
    if any(token in text for token in question_tokens) or any(token in lower for token in question_tokens):
        return False
    if _missionos_instruction_has_route_expression(text):
        return True
    planning_tokens = (
        "強風",
        "風",
        "ドローン",
        "飛ば",
        "飛行",
        "配送",
        "ミッション",
        "作成",
        "運ぶ",
        "waypoint",
        "way point",
        "wind",
        "windy",
        "px4",
        "gazebo",
        "sitl",
        "terrain",
        "battery",
    )
    return any(token in text for token in planning_tokens) or any(token in lower for token in planning_tokens)


_MISSIONOS_PLACE_QUERY_HINT_WORDS = frozenset(
    {
        "airport",
        "bridge",
        "building",
        "campus",
        "city",
        "hall",
        "hospital",
        "library",
        "museum",
        "park",
        "port",
        "station",
        "terminal",
        "tower",
        "university",
        "駅",
        "空港",
        "橋",
        "図書館",
        "公園",
        "大学",
        "病院",
        "港",
        "市役所",
    }
)


def _missionos_route_query_looks_like_place(value: str) -> bool:
    query = re.sub(r"\s+", " ", str(value or "")).strip(" \t\n\r.,;:!?")
    if not query:
        return False
    lowered = query.lower()
    if re.search(r"[ぁ-んァ-ン一-龥]", query):
        return True
    if any(ch.isdigit() for ch in query):
        return True
    if any(hint in lowered for hint in _MISSIONOS_PLACE_QUERY_HINT_WORDS):
        return True
    words = [word for word in re.split(r"\s+", query) if word]
    return bool(words) and any(word[:1].isupper() for word in words)


def _missionos_instruction_has_route_expression(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return False
    if re.search(r"\S+\s*(?:->|=>|→|⇒)\s*\S+", normalized):
        return True
    if re.search(r"\bfrom\s+.+\s+\bto\s+.+", normalized, flags=re.IGNORECASE):
        return True
    japanese_match = re.search(
        r"(?P<origin>\S+)\s*から\s*(?P<destination>\S+)\s*まで",
        normalized,
    )
    if japanese_match:
        return (
            _missionos_route_query_looks_like_place(japanese_match.group("origin"))
            and _missionos_route_query_looks_like_place(
                japanese_match.group("destination")
            )
        )
    plain_match = re.search(
        r"^\s*(?P<origin>[^,;]+?)\s+\bto\b\s+(?P<destination>[^,;]+?)\s*$",
        normalized,
        flags=re.IGNORECASE,
    )
    if plain_match:
        return (
            _missionos_route_query_looks_like_place(plain_match.group("origin"))
            and _missionos_route_query_looks_like_place(plain_match.group("destination"))
        )
    return False


def _missionos_instruction_intent(text: str) -> str:
    lower = text.lower()
    if any(token in text for token in ("状況", "どういう", "なにが", "何が", "状態", "いま", "今")) or any(
        token in lower for token in ("status", "what happened", "explain")
    ):
        return "status"
    if any(token in text for token in ("飛ばせない", "飛べない", "できない", "できますか", "できる？")) or any(
        token in lower for token in ("can you", "can't", "cannot")
    ):
        return "status"
    if any(token in text for token in ("承認", "進めていい", "進めて")) or any(
        token in lower for token in ("approve", "approved")
    ):
        return "approve"
    if any(token in text for token in ("拒否", "却下", "止め")) or any(
        token in lower for token in ("reject", "deny", "stop")
    ):
        return "reject"
    if any(token in text for token in ("修正", "直して", "やり直")) or any(
        token in lower for token in ("revision", "revise", "change")
    ):
        return "revision"
    if any(token in text for token in ("実行", "走らせ", "開始")) or any(
        token in lower for token in ("run", "execute")
    ):
        return "execute"
    if any(token in text for token in ("修復", "診断", "原因")) or any(
        token in lower for token in ("repair", "diagnose", "debug")
    ):
        return "repair"
    if _missionos_instruction_requests_designer_plan(text):
        return "mission_designer_plan"
    return "plan"


def _missionos_instruction_is_ambiguous_short_reply(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) > 2:
        return False
    return not _missionos_instruction_requests_designer_plan(stripped)


def _missionos_review_approved(review: Mapping[str, Any]) -> bool:
    human_review = review.get("human_operator_review")
    if not isinstance(human_review, Mapping):
        human_review = {}
    return (
        review.get("summary_status") == "approved"
        and human_review.get("human_operator_approval_granted_in_artifact") is True
    )


def _missionos_action_blocking_reasons(payload: Mapping[str, Any]) -> list[str]:
    boundary = payload.get("authority_boundary")
    if not isinstance(boundary, Mapping):
        boundary = {}
    reasons: list[str] = []
    for source in (payload.get("blocking_reasons"), boundary.get("blocking_reasons")):
        if isinstance(source, list):
            reasons.extend(str(item) for item in source if str(item or ""))
    return list(dict.fromkeys(reasons))


def _missionos_blocked_action_message(payload: Mapping[str, Any]) -> str:
    reasons = _missionos_action_blocking_reasons(payload)
    if "RUN_MISSIONOS_SITL_DISPATCH_RUNTIME_not_enabled" in reasons:
        if os.getenv("RUN_MISSIONOS_SITL_DISPATCH_RUNTIME") == "1":
            return (
                "The latest persisted action evidence was blocked when SITL dispatch was not opted in, "
                "but this Gateway is now running with RUN_MISSIONOS_SITL_DISPATCH_RUNTIME=1. "
                "I did not dispatch or count progress from that old evidence. "
                "Request a fresh bounded mission plan, or explicitly execute the current approved plan if that is still the intended action."
            )
        return (
            "I cannot run the approved action in this Gateway because SITL dispatch is not opted in. "
            "Start the Gateway with RUN_MISSIONOS_SITL_DISPATCH_RUNTIME=1, then ask me to execute again. "
            "I did not dispatch or count progress."
        )
    if f"{FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV}_not_enabled" in reasons:
        return (
            "I cannot verify that action yet because trajectory re-observation is not opted in. "
            f"Set {FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV}=1 for non-payload Form 2a re-observation. "
            "I did not count progress."
        )
    if reasons:
        return (
            "I tried to route the approved action through the executor and verifier gates, "
            f"but the gate blocked it: {', '.join(reasons)}. I did not count progress."
        )
    return "The latest runtime attempt is blocked. I can diagnose it or draft a repair plan."


def _missionos_conversation_status_message(
    selection: Mapping[str, Any],
    review: Mapping[str, Any],
    action: Mapping[str, Any],
) -> str:
    selected = (
        selection.get("selected_response_kind")
        or (selection.get("response_selection") or {}).get("selected_response_kind")
        if isinstance(selection.get("response_selection"), Mapping)
        else None
    ) or "the current plan"
    classification = action.get("classification") if isinstance(action.get("classification"), Mapping) else {}
    if classification.get("ai_agent_progress_counted") is True and _missionos_review_approved(review):
        return f"The approved {selected} plan has verifier-observed mission behavior."
    if action.get("summary_status") and "blocked" in str(action.get("summary_status")):
        return _missionos_blocked_action_message(action)
    if _missionos_review_approved(review):
        return f"You approved {selected}. I can run the bounded action when you explicitly ask me to execute it."
    if selection.get("summary_status") == "form2a_response_selected":
        return f"I am waiting for your decision on {selected}. You can approve, reject, or ask me to revise it."
    return "I do not have a current proposal yet. Tell me what goal or constraint to consider, and I will plan from the latest evidence."


def _missionos_mission_designer_context_message(context: Mapping[str, Any]) -> str:
    summary = context.get("summary") if isinstance(context.get("summary"), Mapping) else {}
    proposal = context.get("scenario_proposal") if isinstance(context.get("scenario_proposal"), Mapping) else {}
    objective = str(
        proposal.get("mission_objective")
        or summary.get("mission_objective")
        or "the current Flight Scenario Designer proposal"
    )
    if context.get("sitl_execution_request"):
        return (
            f"The current Flight Scenario Designer proposal is already prepared for SITL handoff: {objective}. "
            "I have not run Gazebo, uploaded a mission, dispatched, or counted progress."
        )
    if isinstance(context.get("bounded_simulation_request"), Mapping):
        return (
            f"The current Flight Scenario Designer proposal is approved for a bounded simulation request: {objective}. "
            "Type `prepare` in chat, or run `missionos run`, if you want me to prepare the SITL execution request. "
            "I will still not run Gazebo, upload, dispatch, or count progress from that preparation step alone."
        )
    if _missionos_mission_designer_has_proposal(context):
        return (
            f"The current Flight Scenario Designer proposal is waiting for human approval: {objective}. "
            "Type `approve` to approve this bounded simulation request, or ask me to revise it."
        )
    return ""


_SENSITIVE_INTENTS = frozenset({"approve", "reject", "execute"})


def _build_missionos_router_state(
    selection: Mapping[str, Any],
    review: Mapping[str, Any],
    action: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a compact MissionOS state summary for the LLM Dialogue Router."""
    classification = action.get("classification") if isinstance(action.get("classification"), Mapping) else {}
    human_review = review.get("human_operator_review") if isinstance(review.get("human_operator_review"), Mapping) else {}
    return {
        "selection_status": str(selection.get("summary_status") or "missing"),
        "selected_response_kind": selection.get("selected_response_kind"),
        "review_status": str(review.get("summary_status") or "missing"),
        "approval_granted": bool(human_review.get("human_operator_approval_granted_in_artifact")),
        "action_status": str(action.get("summary_status") or "missing"),
        "ai_agent_progress_counted": bool(classification.get("ai_agent_progress_counted")),
        "action_blocking_reasons": _missionos_action_blocking_reasons(action),
    }


def _missionos_agent_invocation_present(
    runtime_result: Mapping[str, Any],
    agent_name: str,
) -> bool:
    invocations = runtime_result.get("agent_invocations")
    if not isinstance(invocations, list):
        return False
    return any(
        isinstance(invocation, Mapping)
        and invocation.get("agent_name") == agent_name
        and invocation.get("provider") == "google_adk_gemini"
        for invocation in invocations
    )


def _missionos_agent_invocation_artifact_ref(
    runtime_result: Mapping[str, Any],
    agent_name: str,
) -> str:
    invocations = runtime_result.get("agent_invocations")
    if not isinstance(invocations, list):
        return ""
    for invocation in invocations:
        if not isinstance(invocation, Mapping):
            continue
        if invocation.get("agent_name") != agent_name:
            continue
        artifact_path = str(invocation.get("artifact_path") or "")
        if artifact_path:
            return f"missionos_agent_invocation_evidence:{artifact_path}"
    return ""


def _missionos_capability_context_from_agent_runtime(
    *,
    capability_id: str,
    agent_runtime_result: Mapping[str, Any],
    specialist_agent_name: str = "",
    request_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return capability_invocation_context(
        capability_id,
        requested_by=(
            "missionos_chief_agent"
            if _missionos_agent_invocation_present(
                agent_runtime_result,
                "missionos_chief_agent",
            )
            else "direct_gateway_route"
        ),
        source_route="/missionos/autonomy-conversation/run",
        chief_agent_invocation_ref=_missionos_agent_invocation_artifact_ref(
            agent_runtime_result,
            "missionos_chief_agent",
        ),
        specialist_agent_invocation_ref=(
            _missionos_agent_invocation_artifact_ref(
                agent_runtime_result,
                specialist_agent_name,
            )
            if specialist_agent_name
            else ""
        ),
        safety_critic_ref=_missionos_agent_invocation_artifact_ref(
            agent_runtime_result,
            "missionos_safety_critic_agent",
        ),
        request_payload=request_payload or {},
    )


def _missionos_internal_capability_route_response(
    payload: Mapping[str, Any],
    *,
    capability_id: str,
    source_route: str,
) -> dict[str, Any]:
    capability = dict(MISSIONOS_INTERNAL_CAPABILITIES[capability_id])
    response = dict(payload)
    response["capability_surface"] = {
        "capability_id": capability_id,
        "capability_label": capability.get("label", ""),
        "capability_route": capability.get("route", ""),
        "summary_route": capability.get("summary_route", ""),
        "operator_facing": False,
        "internal_capability": True,
        "direct_route_compatibility": True,
        "compatibility_route_status": "compatibility_route_retained",
        "source_route": source_route,
        "preferred_operator_entrypoint": MISSIONOS_OPERATOR_FACING_ROUTE,
        "chief_invokes_tools_directly": False,
    }
    return response


def _missionos_repair_capability_handoff_response(
    payload: Mapping[str, Any],
    *,
    capability_context: Mapping[str, Any],
    input_scope: str,
) -> dict[str, Any]:
    response = _missionos_internal_capability_route_response(
        payload,
        capability_id="llm_repair_planning",
        source_route="/missionos/autonomy-conversation/run",
    )
    surface = (
        response.get("capability_surface")
        if isinstance(response.get("capability_surface"), Mapping)
        else {}
    )
    coordinating_agent_invoked = bool(
        str(capability_context.get("specialist_agent_invocation_ref") or "")
    )
    response["capability_surface"] = {
        **dict(surface),
        "capability_handoff": True,
        "coordinating_agent": "missionos_repair_planner_agent",
        "coordinating_agent_invoked": coordinating_agent_invoked,
        "coordinator_role": "repair_coordinator_agent",
        "capability_invocation_ref": capability_context.get(
            "capability_invocation_ref",
            "",
        ),
        "repair_phase": "post_block_or_next_run_planning",
        "input_scope": input_scope,
        "in_flight_recovery_owner": "missionos_runtime_recovery_agent",
        "approval_or_execution_created": False,
    }
    response["repair_agent_handoff"] = {
        "schema_version": "missionos_repair_agent_capability_handoff.v1",
        "coordinating_agent": "missionos_repair_planner_agent",
        "coordinating_agent_invoked": coordinating_agent_invoked,
        "capability_id": "llm_repair_planning",
        "capability_route": "/missionos/llm-repair-planner/run",
        "capability_invocation_ref": capability_context.get(
            "capability_invocation_ref",
            "",
        ),
        "repair_phase": "post_block_or_next_run_planning",
        "input_scope": input_scope,
        "active_in_flight_recovery_owner": "missionos_runtime_recovery_agent",
        "human_approval_required_for_execution": True,
        "approval_or_execution_created": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }
    return response


def _missionos_repair_evidence_from_mission_designer_context(
    context: Mapping[str, Any],
    *,
    operator_instruction: str,
) -> dict[str, Any]:
    if not context or _missionos_mission_designer_context_error(context):
        return {}
    has_repair_relevant_context = any(
        isinstance(context.get(key), Mapping)
        for key in (
            "scenario_proposal",
            "validation_result",
            "scenario_approval",
            "bounded_simulation_request",
            "sitl_execution_request",
            "missionos_payload_split_plan",
            "mission_designer_coordinate_pair_route",
        )
    )
    if not has_repair_relevant_context:
        return {}

    summary = context.get("summary") if isinstance(context.get("summary"), Mapping) else {}
    chief_tools = (
        context.get("missionos_chief_planner_internal_tools")
        if isinstance(context.get("missionos_chief_planner_internal_tools"), Mapping)
        else {}
    )
    route = chief_tools.get("coordinate_route")
    if not isinstance(route, Mapping):
        route = context.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, Mapping) else {}

    payload_split_plan = (
        context.get("missionos_payload_split_plan")
        if isinstance(context.get("missionos_payload_split_plan"), Mapping)
        else {}
    )
    blocking_reasons = [
        str(reason)
        for reason in (
            summary.get("blocked_reasons")
            or summary.get("blocking_reasons")
            or context.get("blocked_reasons")
            or []
        )
    ]
    wind_speed = route.get("wind_speed_mps")
    if (
        isinstance(wind_speed, (int, float))
        and wind_speed > MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS
    ):
        blocking_reasons.append("wind_over_live_sitl_contract")
    if payload_split_plan.get("plan_status") == "split_required":
        blocking_reasons.append("payload_split_required")
    blocking_reasons = list(dict.fromkeys(item for item in blocking_reasons if item))

    task_id = (
        summary.get("sitl_execution_task_id")
        or context.get("sitl_execution_task_id")
        or ""
    )
    task = context.get("sitl_execution_task")
    if isinstance(task, Mapping) and task.get("task_id"):
        task_id = task.get("task_id")

    return {
        "schema_version": "missionos_mission_designer_context_repair_input.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_label": "mission_designer_context",
        "summary_status": "blocked" if blocking_reasons else "context_attached",
        "blocking_reasons": blocking_reasons,
        "operator_instruction": operator_instruction[:2000],
        "task_id": str(task_id or ""),
        "repair_phase": "post_block_or_next_run_planning",
        "route_constraints": dict(route),
        "payload_split_plan": dict(payload_split_plan),
        "mission_designer_context": dict(context),
        "source_boundary": {
            "source": "gateway_resolved_mission_designer_context",
            "client_supplied_evidence_trusted": False,
            "context_ref_verified_server_side": True,
            "operator_approved": False,
            "dispatch_authority_created": False,
            "progress_counted": False,
        },
    }


def _missionos_repair_prompt_from_evidence(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    blocking_reasons = [
        str(reason)
        for reason in evidence.get("blocking_reasons") or []
        if str(reason)
    ]
    if not blocking_reasons:
        return {}
    return {
        "schema_version": "missionos_repair_prompt.v1",
        "prompt_status": "repair_available",
        "blocking_reasons": blocking_reasons,
        "operator_prompt": (
            "Repair Agent can draft a next-run repair proposal. "
            "Type `/repair` to analyze this blocked evidence."
        ),
        "suggested_command": "/repair",
        "repair_phase": "post_block_or_next_run_planning",
        "dispatch_authority_created": False,
        "progress_counted": False,
    }


def _missionos_repair_followup_warnings(evidence: Mapping[str, Any]) -> list[str]:
    blocking_reasons = {
        str(reason)
        for reason in evidence.get("blocking_reasons") or []
        if str(reason)
    }
    warnings: list[str] = []
    if "wind_over_live_sitl_contract" in blocking_reasons:
        warnings.append(
            "Live SITL remains blocked until wind is within the "
            f"{MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS:.1f}m/s contract; "
            "the Repair Agent proposal does not approve, dispatch, or execute."
        )
    if "payload_split_required" in blocking_reasons:
        warnings.append(
            "Payload split remains a planning repair item and still requires "
            "normal human approval before any follow-up run."
        )
    return warnings


def _missionos_agent_invocation_names(invocations: list[Any]) -> set[str]:
    return {
        str(invocation.get("agent_name") or "")
        for invocation in invocations
        if isinstance(invocation, Mapping)
    }


def _missionos_gateway_fallback_safety_critic(
    *,
    intent: str,
    routing_source: str,
    operator_instruction: str,
    agent_runtime_result: Mapping[str, Any],
    keyword_intent: str,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    runtime_blocks = [
        str(reason)
        for reason in agent_runtime_result.get("blocking_reasons") or []
    ]
    authority_adjacent = intent in (_SENSITIVE_INTENTS | {"repair", "mission_designer_plan"})
    boundary_status = "operator_review_required" if authority_adjacent else "safe"
    required_checks = [
        "keyword_intent_confirmation",
        "gateway_authority_boundary",
    ]
    if intent in _SENSITIVE_INTENTS:
        required_checks.append("approval_token_or_execution_gate_check")
    if intent == "repair":
        required_checks.append("repair_proposal_guardrail")
    if intent == "mission_designer_plan":
        required_checks.append("mission_designer_planning_guardrail")
    findings = [
        f"agent_runtime_status:{agent_runtime_result.get('runtime_status') or 'missing'}",
        f"routing_source:{routing_source}",
        f"keyword_intent:{keyword_intent}",
        "deterministic_fallback_boundary_review",
    ]
    findings.extend(f"agent_runtime_block:{reason}" for reason in runtime_blocks)
    return {
        "schema_version": "missionos_gateway_fallback_safety_critic.v1",
        "agent_name": "missionos_gateway_fallback_safety_critic",
        "agent_role": "MissionOS deterministic fallback boundary critic",
        "provider": "gateway_deterministic_fallback",
        "record_purpose": "fallback_boundary_review_evidence",
        "record_is_gate": False,
        "blocks_dispatch_by_itself": False,
        "downstream_deterministic_gates_remain_required": True,
        "invocation_started_at": started_at,
        "invocation_finished_at": started_at,
        "guardrail_result": {"guardrail_passed": True, "blocking_reasons": []},
        "validated_output": {
            "intent": intent,
            "operator_instruction": operator_instruction[:2000],
            "boundary_status": boundary_status,
            "review_record_type": "deterministic_fallback_boundary_review",
            "record_is_gate": False,
            "boundary_findings": findings,
            "required_gateway_checks": required_checks,
            "rationale": (
                "Gateway applied a deterministic boundary review because the "
                "MissionOS agent runtime did not produce a guardrail-passed "
                "proposal before keyword fallback routing. This record is "
                "evidence for existing Gateway authority boundaries; it does "
                "not block dispatch by itself."
            ),
            "requires_human_approval": authority_adjacent,
            "uncertainty": "",
        },
        "source_agent_runtime_status": agent_runtime_result.get("runtime_status") or "",
        "source_agent_runtime_blocking_reasons": runtime_blocks,
        "routing_source": routing_source,
        "progress_counted": False,
        "llm_judgment_in_gate": False,
        "dispatch_authority_created": False,
    }


def run_missionos_autonomy_conversation(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Route a plain-language operator instruction through existing MissionOS gates.

    Tries the LLM Dialogue Router first. If unavailable, falls back to keyword
    routing. If the LLM output fails the guardrail, returns an error to the
    human rather than silently substituting keyword routing (Case B).
    """
    request = dict(payload or {})
    text = _missionos_instruction_text(request)
    client_surface = _missionos_client_surface(request)
    session_id = _missionos_request_session_id(request)
    mission_designer_context = _missionos_mission_designer_context(request)
    coordinate_route = request.get("coordinate_route") if isinstance(request.get("coordinate_route"), Mapping) else None
    chief_planner_tools: dict[str, Any] = {}
    if coordinate_route is None and _missionos_instruction_requests_designer_plan(text):
        chief_planner_tools = resolve_chief_planner_internal_tools(
            utterance=text,
            now=datetime.now(timezone.utc),
        )
        tool_coordinate_route = chief_planner_tools.get("coordinate_route")
        if isinstance(tool_coordinate_route, Mapping):
            coordinate_route = tool_coordinate_route
        else:
            existing_chief_tools = mission_designer_context.get(
                "missionos_chief_planner_internal_tools"
            )
            existing_chief_tools = (
                existing_chief_tools if isinstance(existing_chief_tools, Mapping) else {}
            )
            existing_route = existing_chief_tools.get("coordinate_route")
            if not isinstance(existing_route, Mapping):
                existing_route = mission_designer_context.get(
                    "mission_designer_coordinate_pair_route"
                )
            overrides = extract_operator_requested_route_overrides(text)
            if isinstance(existing_route, Mapping) and overrides:
                try:
                    auto_route_waypoint_count = int(
                        existing_route.get("auto_route_waypoint_count") or 20
                    )
                except (TypeError, ValueError):
                    auto_route_waypoint_count = 20
                coordinate_route = {
                    **dict(existing_route),
                    **overrides,
                    "auto_route_waypoint_count": auto_route_waypoint_count,
                    "route_source": "missionos_chief_followup_route_parameter_update",
                    "route_revision_source": "operator_followup_instruction",
                    "operator_facing_agent": "missionos_chief_agent",
                    "subagents_operator_facing": False,
                    "dispatch_authority_created": False,
                    "progress_counted": False,
                }
                chief_planner_tools = {
                    "schema_version": "missionos_chief_planner_internal_tools.v1",
                    "tool_status": "resolved",
                    "operator_facing_agent": "missionos_chief_agent",
                    "subagents_operator_facing": False,
                    "internal_tool_names": [
                        "missionos_route_parameter_update_tool",
                    ],
                    "route_parameter_update": {
                        "schema_version": "missionos_route_parameter_update_tool_result.v1",
                        "tool_name": "missionos_route_parameter_update_tool",
                        "tool_status": "resolved",
                        "updated_fields": sorted(overrides),
                        "dispatch_authority_created": False,
                        "progress_counted": False,
                    },
                    "coordinate_route": coordinate_route,
                    "dispatch_authority_created": False,
                    "progress_counted": False,
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
    if (
        isinstance(coordinate_route, Mapping)
        and not coordinate_route.get("terrain_profile")
    ):
        enriched_route, terrain_tool = enrich_coordinate_route_with_terrain_profile(
            coordinate_route,
            now=datetime.now(timezone.utc),
        )
        coordinate_route = enriched_route
        if terrain_tool:
            terrain_profile = (
                coordinate_route.get("terrain_profile")
                if isinstance(coordinate_route, Mapping)
                else None
            )
            tool_status = "resolved" if terrain_profile else "partial"
            existing_tool_names = list(
                chief_planner_tools.get("internal_tool_names") or []
            )
            if "missionos_terrain_elevation_resolver_tool" not in existing_tool_names:
                existing_tool_names.append("missionos_terrain_elevation_resolver_tool")
            chief_planner_tools = {
                **chief_planner_tools,
                "schema_version": "missionos_chief_planner_internal_tools.v1",
                "tool_status": chief_planner_tools.get("tool_status") or tool_status,
                "operator_facing_agent": "missionos_chief_agent",
                "subagents_operator_facing": False,
                "internal_tool_names": existing_tool_names,
                "terrain_resolver": terrain_tool,
                "coordinate_route": coordinate_route,
                "dispatch_authority_created": False,
                "progress_counted": False,
                "resolved_at": chief_planner_tools.get("resolved_at")
                or datetime.now(timezone.utc).isoformat(),
            }
    payload_split_plan: dict[str, Any] = {}
    if isinstance(coordinate_route, Mapping):
        payload_split_plan = _missionos_payload_split_plan_for_route(coordinate_route)
        if payload_split_plan.get("plan_status") == "split_required":
            coordinate_route = apply_payload_split_plan_to_coordinate_route(
                coordinate_route=coordinate_route,
                payload_split_plan=payload_split_plan,
            )
            chief_planner_tools = _missionos_attach_payload_split_plan_to_tools(
                chief_planner_tools,
                coordinate_route=coordinate_route,
                payload_split_plan=payload_split_plan,
            )
    conversation_history = request.get("conversation_history") or []
    if not isinstance(conversation_history, list):
        conversation_history = []
    monitoring_observations = (
        request.get("missionos_monitoring_observations")
        or request.get("monitoring_observations")
        or []
    )
    if not isinstance(monitoring_observations, list):
        monitoring_observations = []
    route_hint = str(request.get("missionos_route_hint") or "").strip()

    selection = build_form2a_response_selection_summary()
    review = build_form2a_operator_review_summary()
    action = build_form2a_action_consumption_summary()

    missionos_state = _build_missionos_router_state(selection, review, action)
    keyword_intent = _missionos_instruction_intent(text)
    agent_runtime_result = run_missionos_agent_runtime(
        utterance=text,
        missionos_state=missionos_state,
        mission_designer_context=mission_designer_context,
        coordinate_route=coordinate_route,
        conversation_history=conversation_history,
        monitoring_observations=monitoring_observations,
        route_hint=route_hint,
        timeout_seconds=_missionos_conversation_agent_timeout_seconds(),
    )
    agent_runtime_status = agent_runtime_result.get("runtime_status")
    router_result: dict[str, Any] = {}

    if agent_runtime_status == "proposal_guardrail_passed":
        proposal = agent_runtime_result["proposal"]
        intent = proposal["intent"]
        enriched_instruction = proposal["operator_instruction"] or text
        routing_source = "missionos_agent_runtime"
        # Sensitive intents (approve/reject/execute) require keyword confirmation.
        # If the original utterance does not deterministically match via the
        # keyword router, downgrade to clarification to prevent LLM/Agent interpretation
        # alone from triggering approval or execution paths.
        if intent in _SENSITIVE_INTENTS and keyword_intent != intent:
            intent = "clarification"
            routing_source = "missionos_agent_runtime_sensitive_intent_downgraded"
        elif intent == "plan" and keyword_intent == "mission_designer_plan":
            intent = "mission_designer_plan"
            routing_source = "missionos_agent_runtime_designer_boundary_corrected"
        elif intent in {"plan", "mission_designer_plan"} and keyword_intent == "status":
            intent = "status"
            enriched_instruction = text
            routing_source = "missionos_agent_runtime_status_boundary_corrected"
    else:
        # A blocked Agent output is evidence about that Agent invocation, not an
        # automatic operator-facing conversation block. Non-sensitive,
        # deterministic localized flight intents must still be allowed
        # to fall through to Gateway guardrails / keyword routing. Sensitive
        # approval or execution remains protected by the keyword checks below.
        router_result = run_llm_dialogue_router(text, missionos_state, conversation_history)
        router_status = router_result.get("router_status")

        if router_status == "guardrail_blocked":
            return {
                "schema_version": "missionos_autonomy_conversation_response.v1",
                "operator_instruction": {"text": text, "source": "missionos_autonomy_monitor"},
                "routed_action": "router_rejected",
                "routing_source": "llm_dialogue_router",
                "message": (
                    "MissionOS could not safely interpret that instruction. "
                    "The router proposal was blocked by the guardrail. "
                    "I did not approve, dispatch, or count progress. "
                    "Please rephrase your instruction."
                ),
                "operation_result": {},
                "selection": selection,
                "review": review,
                "action": action,
                "repair": build_llm_repair_planner_summary(),
                "progress_counted": False,
                "conversation_route_bypassed_guardrails": False,
                "dialogue_router": router_result,
                "missionos_agent_runtime": agent_runtime_result,
                "missionos_agent_invocations": list(agent_runtime_result.get("agent_invocations") or []),
                "missionos_monitoring_observations": list(
                    agent_runtime_result.get("monitoring_observations") or []
                ),
            }

        if router_status == "proposal_guardrail_passed":
            proposal = router_result["proposal"]
            intent = proposal["intent"]
            enriched_instruction = proposal["operator_instruction"] or text
            routing_source = "llm_dialogue_router"
            if intent in _SENSITIVE_INTENTS and keyword_intent != intent:
                intent = "clarification"
                routing_source = "llm_dialogue_router_sensitive_intent_downgraded"
            elif intent == "plan" and keyword_intent == "mission_designer_plan":
                intent = "mission_designer_plan"
                routing_source = "llm_dialogue_router_designer_boundary_corrected"
            elif intent in {"plan", "mission_designer_plan"} and keyword_intent == "status":
                intent = "status"
                enriched_instruction = text
                routing_source = "llm_dialogue_router_status_boundary_corrected"
        else:
            intent = keyword_intent
            enriched_instruction = text
            routing_source = "keyword_fallback"

    if keyword_intent in _SENSITIVE_INTENTS and intent != keyword_intent:
        intent = keyword_intent
        enriched_instruction = text
        routing_source = f"{routing_source}_keyword_sensitive_intent_corrected"

    if (
        keyword_intent == "execute"
        and _missionos_instruction_requests_designer_plan(text)
        and (
            chief_planner_tools.get("tool_status") in {"resolved", "partial"}
            or not _missionos_mission_designer_has_proposal(mission_designer_context)
        )
    ):
        intent = "mission_designer_plan"
        enriched_instruction = text
        routing_source = f"{routing_source}_mixed_execute_designer_request_planning_first"

    if (
        keyword_intent == "plan"
        and intent in {"plan", "mission_designer_plan"}
        and _missionos_instruction_is_ambiguous_short_reply(text)
    ):
        intent = "status"
        enriched_instruction = text
        routing_source = f"{routing_source}_ambiguous_short_status_corrected"

    if (
        coordinate_route
        and keyword_intent == "plan"
        and intent in {"plan", "status"}
        and _missionos_instruction_requests_designer_plan(text)
        and not _missionos_mission_designer_has_proposal(mission_designer_context)
    ):
        intent = "mission_designer_plan"
        enriched_instruction = text
        routing_source = f"{routing_source}_coordinate_route_mission_designer_plan"

    if (
        route_hint == "mission_designer_plan"
        and intent not in _SENSITIVE_INTENTS
        and keyword_intent not in _SENSITIVE_INTENTS
    ):
        intent = "mission_designer_plan"
        enriched_instruction = text
        routing_source = f"{routing_source}_route_hint_mission_designer_plan"

    if (
        chief_planner_tools.get("tool_status") in {"resolved", "partial"}
        and keyword_intent != "status"
        and (
            (intent not in _SENSITIVE_INTENTS and keyword_intent not in _SENSITIVE_INTENTS)
            or keyword_intent == "execute"
        )
    ):
        intent = "mission_designer_plan"
        enriched_instruction = text
        routing_source = f"{routing_source}_chief_internal_route_weather_tools"

    missionos_agent_invocations = list(agent_runtime_result.get("agent_invocations") or [])
    fallback_safety_critic: dict[str, Any] = {}
    if (
        agent_runtime_status != "proposal_guardrail_passed"
        and "missionos_safety_critic_agent"
        not in _missionos_agent_invocation_names(missionos_agent_invocations)
        and intent not in {"clarification", "router_rejected"}
    ):
        fallback_safety_critic = _missionos_gateway_fallback_safety_critic(
            intent=intent,
            routing_source=routing_source,
            operator_instruction=enriched_instruction,
            agent_runtime_result=agent_runtime_result,
            keyword_intent=keyword_intent,
        )
        missionos_agent_invocations.append(fallback_safety_critic)

    result: dict[str, Any] | None = None
    message = ""
    missionos_repair_prompt: dict[str, Any] = {}
    context_error = _missionos_mission_designer_context_error(mission_designer_context)

    try:
        if context_error and intent in {"approve", "reject", "execute", "status", "clarification"}:
            result = mission_designer_context
            message = (
                "I cannot use the Mission Designer context sent by the browser because it is not "
                f"source-bound to this Gateway session ({context_error}). "
                "Please ask me to create a fresh flight scenario plan, then approve that current plan. "
                "I did not approve, prepare SITL, dispatch, or count progress."
            )
        elif intent == "clarification":
            llm_proposed = (router_result.get("proposal") or {}).get("intent", "")
            context_message = _missionos_mission_designer_context_message(mission_designer_context)
            result = mission_designer_context if context_message else None
            if context_message:
                message = (
                    f"{context_message} "
                    f"I understood you may want to {llm_proposed}, but that wording was not explicit enough for a sensitive action. "
                    "Please use explicit action words such as `approve`, `reject`, or `execute`."
                )
            else:
                message = (
                    f"I understood you may want to {llm_proposed}, "
                    "but I need an explicit instruction to proceed with that action. "
                    "Please use a direct phrase such as `approve`, `reject`, or `execute`."
                )
        elif intent == "status":
            context_message = _missionos_mission_designer_context_message(mission_designer_context)
            if context_message:
                result = mission_designer_context
                message = context_message
            else:
                message = _missionos_conversation_status_message(selection, review, action)
        elif intent == "approve":
            approval_context = _missionos_capability_context_from_agent_runtime(
                capability_id="form2a_operator_review",
                agent_runtime_result=agent_runtime_result,
                request_payload={"intent": intent, "operator_instruction": text},
            )
            if _missionos_mission_designer_has_proposal(mission_designer_context):
                approval_result = approve_px4_gazebo_mission_scenario_for_bounded_simulation(
                    proposal=mission_designer_context["scenario_proposal"],
                    validation=mission_designer_context["validation_result"],
                    now=datetime.now(timezone.utc),
                )
                result = _missionos_register_mission_designer_context(
                    _missionos_merge_mission_designer_context(
                        mission_designer_context,
                        approval_result,
                    ),
                    session_id=session_id,
                )
                message = (
                    "I recorded your approval against the current Flight Scenario Designer proposal. "
                    "The scope is bounded simulation request only; I did not prepare SITL, dispatch, or count progress."
                )
                if client_surface == "chat":
                    message = (
                        "Approval recorded. I have not prepared SITL, dispatched, or counted progress. "
                        "Prepare the SITL execution request? Type `prepare` to continue."
                    )
            elif selection.get("summary_status") == "form2a_response_selected":
                result = run_form2a_operator_review_approve(
                    capability_context=approval_context
                )
                message = "I recorded your approval against the current source-bound plan."
            else:
                message = "I cannot approve yet because there is no current bounded plan."
        elif intent == "reject":
            approval_context = _missionos_capability_context_from_agent_runtime(
                capability_id="form2a_operator_review",
                agent_runtime_result=agent_runtime_result,
                request_payload={"intent": intent, "operator_instruction": text},
            )
            if _missionos_mission_designer_has_proposal(mission_designer_context):
                result = _missionos_register_mission_designer_context(
                    _missionos_merge_mission_designer_context(
                        mission_designer_context,
                        {
                        "scenario_rejection": {
                            "schema_version": "missionos_flight_scenario_operator_rejection.v1",
                            "rejection_status": "rejected",
                            "operator_rejected": True,
                            "rejected_at": datetime.now(timezone.utc).isoformat(),
                            "dispatch_authority_created": False,
                            "gazebo_execution_invoked": False,
                            "progress_counted": False,
                        },
                        "summary": {
                            "approval_status": "rejected",
                            "operator_rejected": True,
                            "dispatch_authority_created": False,
                            "gazebo_execution_invoked": False,
                            "progress_counted": False,
                        },
                        },
                    ),
                    session_id=session_id,
                )
                message = (
                    "I recorded your rejection against the current Flight Scenario Designer proposal. "
                    "I will not prepare SITL, dispatch, or count progress for it."
                )
            elif selection.get("summary_status") == "form2a_response_selected":
                result = run_form2a_operator_review_reject(
                    capability_context=approval_context
                )
                message = "I recorded your rejection. I will not execute that plan."
            else:
                message = "I cannot reject a plan because there is no current bounded plan."
        elif intent == "revision":
            if selection.get("summary_status") == "form2a_response_selected":
                result = run_form2a_operator_review_request_revision(
                    capability_context=_missionos_capability_context_from_agent_runtime(
                        capability_id="form2a_operator_review",
                        agent_runtime_result=agent_runtime_result,
                        request_payload={"intent": intent, "operator_instruction": text},
                    )
                )
                message = "I recorded your revision request. I will not execute the current plan."
            else:
                result = run_form2a_response_selection_from_form1(
                    operator_instruction=enriched_instruction,
                    capability_context=_missionos_capability_context_from_agent_runtime(
                        capability_id="form2a_response_selection",
                        agent_runtime_result=agent_runtime_result,
                        specialist_agent_name="missionos_response_planner_agent",
                        request_payload={
                            "intent": intent,
                            "operator_instruction": enriched_instruction,
                        },
                    ),
                )
                message = "I asked the planner for a bounded plan from your instruction."
        elif intent == "execute":
            if mission_designer_context.get("sitl_execution_request"):
                result = mission_designer_context
                message = (
                    "The current Flight Scenario Designer proposal already has a prepared SITL execution request. "
                    "I did not run Gazebo, upload a mission, dispatch, or count progress. "
                    "Live SITL execution still requires explicit server opt-in and the separate execution gate."
                )
            elif isinstance(mission_designer_context.get("bounded_simulation_request"), Mapping):
                result = _missionos_prepare_mission_designer_sitl_context(mission_designer_context)
                message = (
                    "I prepared the Mission Designer SITL execution request for the approved scenario. "
                    "I did not run Gazebo, upload a mission, dispatch, or count progress. "
                    "Live SITL execution still requires explicit server opt-in and the separate execution gate."
                )
                if client_surface == "chat":
                    message = (
                        "SITL execution request prepared from the approved proposal. "
                        "I have not started PX4/Gazebo, uploaded a mission, dispatched, or counted progress. "
                        "Start PX4/Gazebo SITL? Type `start` to continue."
                    )
            elif _missionos_mission_designer_has_proposal(mission_designer_context):
                message = (
                    "I can prepare this Flight Scenario Designer proposal only after you explicitly approve it. "
                    "Please say `approve` if you want to approve this bounded simulation request."
                )
                if client_surface == "chat":
                    message = (
                        "This proposal can become a SITL execution request only after explicit approval. "
                        "Type `approve` to approve it."
                    )
            elif _missionos_review_approved(review):
                result = run_form2a_action_consumption()
                action_after = build_form2a_action_consumption_summary()
                if str(action_after.get("summary_status") or "").lower() == "blocked":
                    message = _missionos_blocked_action_message(action_after)
                else:
                    message = "I ran the approved bounded action through the executor and verifier gates."
            else:
                message = "I can run only after you approve a current bounded plan."
        elif intent == "repair":
            repair_capability_context = _missionos_capability_context_from_agent_runtime(
                capability_id="llm_repair_planning",
                agent_runtime_result=agent_runtime_result,
                specialist_agent_name="missionos_repair_planner_agent",
                request_payload={"intent": intent, "operator_instruction": text},
            )
            repair_input_scope = "latest_blocked_or_failed_evidence"
            repair_context_evidence = _missionos_repair_evidence_from_mission_designer_context(
                mission_designer_context,
                operator_instruction=text,
            )
            if repair_context_evidence:
                repair_input_scope = "mission_designer_context"
                repair_result = run_llm_repair_planner_from_evidence_payload(
                    evidence_artifact=repair_context_evidence,
                    evidence_label="mission_designer_context",
                    capability_context=repair_capability_context,
                )
            else:
                repair_result = run_llm_repair_planner_from_latest_evidence(
                    capability_context=repair_capability_context
                )
            result = _missionos_repair_capability_handoff_response(
                repair_result,
                capability_context=repair_capability_context,
                input_scope=repair_input_scope,
            )
            repair_followup_warnings = _missionos_repair_followup_warnings(
                repair_context_evidence
            )
            if repair_followup_warnings:
                result = {
                    **result,
                    "repair_followup_warnings": repair_followup_warnings,
                }
            if result["repair_agent_handoff"]["coordinating_agent_invoked"]:
                evidence_label = (
                    "current Mission Designer evidence"
                    if repair_input_scope == "mission_designer_context"
                    else "latest blocked evidence"
                )
                message = (
                    f"I asked the Repair Agent to hand the {evidence_label} to "
                    "the LLM Repair Planner capability."
                )
            else:
                message = (
                    "I sent the repair request to the LLM Repair Planner capability "
                    "through the Gateway fallback path."
                )
            if repair_followup_warnings:
                message = f"{message} {' '.join(repair_followup_warnings)}"
        elif intent == "mission_designer_plan":
            designer_prompt = text if coordinate_route else (enriched_instruction or text)
            designer_result = run_px4_gazebo_mission_scenario_designer(
                prompt=designer_prompt,
                coordinate_route=coordinate_route,
                now=datetime.now(timezone.utc),
            )
            designer_result = _missionos_attach_payload_split_plan_to_result(
                designer_result,
                payload_split_plan,
            )
            if chief_planner_tools.get("tool_status") in {"resolved", "partial"}:
                designer_result = {
                    **designer_result,
                    "missionos_chief_planner_internal_tools": chief_planner_tools,
                    "missionos_route_resolver_tool_result": chief_planner_tools.get(
                        "route_resolver"
                    ),
                    "missionos_postal_code_resolver_tool_results": chief_planner_tools.get(
                        "postal_code_resolvers"
                    )
                    or [],
                    "missionos_weather_resolver_tool_result": chief_planner_tools.get(
                        "weather_resolver"
                    ),
                    "missionos_terrain_elevation_resolver_tool_result": (
                        chief_planner_tools.get("terrain_resolver")
                    ),
                    "summary": {
                        **(
                            dict(designer_result.get("summary"))
                            if isinstance(designer_result.get("summary"), Mapping)
                            else {}
                        ),
                        "operator_facing_agent": "missionos_chief_agent",
                        "chief_internal_route_weather_tools_status": chief_planner_tools.get(
                            "tool_status"
                        ),
                        "subagents_operator_facing": False,
                    },
                }
            result = _missionos_register_mission_designer_context(
                enrich_terrain_heightmap_preview_fields(designer_result),
                session_id=session_id,
            )
            repair_prompt_evidence = _missionos_repair_evidence_from_mission_designer_context(
                result,
                operator_instruction=enriched_instruction or text,
            )
            missionos_repair_prompt = _missionos_repair_prompt_from_evidence(
                repair_prompt_evidence
            )
            if chief_planner_tools.get("tool_status") in {"resolved", "partial"}:
                route = (
                    chief_planner_tools.get("coordinate_route")
                    if isinstance(chief_planner_tools.get("coordinate_route"), Mapping)
                    else {}
                )
                route_summary = "Mission Designer route"
                if route:
                    route_summary = (
                        f"{route.get('takeoff_label') or 'takeoff'} -> "
                        f"{route.get('dropoff_label') or 'dropoff'}"
                    )
                    if route.get("wind_speed_mps") is not None:
                        route_summary += f", wind={route.get('wind_speed_mps')}m/s"
                    if route.get("payload_weight_kg") is not None:
                        requested_total_payload = route.get("requested_total_payload_weight_kg")
                        if requested_total_payload is not None:
                            route_summary += (
                                f", payload={route.get('payload_weight_kg')}kg/sortie"
                                f" (requested_total={requested_total_payload}kg)"
                            )
                        else:
                            route_summary += f", payload={route.get('payload_weight_kg')}kg"
                warning = ""
                wind_speed = route.get("wind_speed_mps") if route else None
                if (
                    isinstance(wind_speed, (int, float))
                    and wind_speed > MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS
                ):
                    warning = (
                        "\nNote: wind="
                        f"{wind_speed}m/s exceeds the live SITL contract max of "
                        f"{MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS}m/s. "
                        "MissionOS can build this proposal, but `execute-sitl` "
                        "will stop at the envelope gate. Revise the wind value "
                        "within the contract limit before a live run."
                    )
                payload_split_warning = _missionos_payload_split_message(payload_split_plan)
                if missionos_repair_prompt and client_surface == "chat":
                    blocked = ", ".join(
                        missionos_repair_prompt.get("blocking_reasons") or []
                    )
                    next_step = (
                        "Mission blocked: "
                        f"{blocked}. "
                        f"{missionos_repair_prompt['operator_prompt']}"
                    )
                else:
                    next_step = (
                        "Approve this plan? Type `approve` to approve it, or describe "
                        "what you want changed."
                        if client_surface == "chat"
                        else (
                            "Next commands:\n"
                            "  missionos approve\n"
                            "  missionos run\n"
                            "  missionos start-sitl\n"
                            "  missionos execute-sitl --live-flight"
                        )
                    )
                message = (
                    f"I built a bounded PX4/Gazebo mission proposal for {route_summary}. "
                    "I did not approve, prepare SITL, or dispatch. "
                    f"{payload_split_warning}{warning}\n"
                    + next_step
                )
            elif _missionos_agent_invocation_present(
                agent_runtime_result,
                "missionos_flight_scenario_designer_agent",
            ):
                message = (
                    "I asked the MissionOS Flight Scenario Designer Agent for a bounded PX4/Gazebo mission proposal. "
                    "This is planning evidence only; I did not approve, prepare SITL, dispatch, or count progress."
                )
            else:
                message = (
                    "I routed this through the Gateway Mission Designer guardrail and built a bounded PX4/Gazebo mission proposal. "
                    "No Agent label is attached because no MissionOS Flight Scenario Designer ADK invocation evidence is present. "
                    "This is planning evidence only; I did not approve, prepare SITL, dispatch, or count progress."
                )
        else:
            result = run_form2a_response_selection_from_form1(
                operator_instruction=enriched_instruction,
                capability_context=_missionos_capability_context_from_agent_runtime(
                    capability_id="form2a_response_selection",
                    agent_runtime_result=agent_runtime_result,
                    specialist_agent_name="missionos_response_planner_agent",
                    request_payload={
                        "intent": intent,
                        "operator_instruction": enriched_instruction,
                    },
                ),
            )
            message = "I asked the planner for a bounded plan from your instruction."
    except Exception as exc:
        message = (
            "I could not complete that instruction in this running Gateway. "
            "I did not approve, dispatch, or count progress."
        )
        result = {"error": str(exc)}

    return {
        "schema_version": "missionos_autonomy_conversation_response.v1",
        "operator_instruction": {"text": text, "source": "missionos_autonomy_monitor"},
        "routed_action": intent,
        "routing_source": routing_source,
        "message": message,
        "operation_result": result or {},
        "mission_designer": (
            result
            if intent in {"mission_designer_plan", "approve", "reject", "execute", "status", "clarification"}
            and isinstance(result, Mapping)
            and (
                "scenario_proposal" in result
                or "validation_result" in result
                or "sitl_execution_request" in result
                or "mission_designer_context_error" in result
            )
            else {}
        ),
        "selection": build_form2a_response_selection_summary(),
        "review": build_form2a_operator_review_summary(),
        "action": build_form2a_action_consumption_summary(),
        "repair": (
            result
            if intent == "repair" and isinstance(result, Mapping)
            else build_llm_repair_planner_summary()
        ),
        "progress_counted": False,
        "conversation_route_bypassed_guardrails": False,
        "dialogue_router": router_result,
        "missionos_agent_runtime": agent_runtime_result,
        "missionos_agent_invocations": missionos_agent_invocations,
        "missionos_monitoring_observations": list(
            agent_runtime_result.get("monitoring_observations") or []
        ),
        "missionos_fallback_safety_critic": fallback_safety_critic,
        "missionos_boundary_reviews": (
            [fallback_safety_critic] if fallback_safety_critic else []
        ),
        "missionos_repair_agent_capability_handoff": (
            result.get("repair_agent_handoff")
            if intent == "repair" and isinstance(result, Mapping)
            else {}
        ),
        "missionos_repair_prompt": missionos_repair_prompt,
        "missionos_chief_planner_internal_tools": (
            chief_planner_tools
            if intent == "mission_designer_plan"
            and chief_planner_tools.get("tool_status") in {"resolved", "partial"}
            else {}
        ),
        "missionos_payload_split_plan": (
            payload_split_plan if intent == "mission_designer_plan" else {}
        ),
    }
from src.gateway.route_utils import normalize_constraints
from src.gateway.task_routes import (
    build_task_router,
    enrich_terrain_heightmap_preview_fields,
)
from src.gateway.audit_routes import build_audit_router
from src.gateway.ws_handler import build_websocket_router
from src.runtime.tool_events import set_tool_event_notifier
from src.runtime.session_service import create_session_service, describe_session_backend
from src.runtime.state_keys import StateKeys
from src.runtime.task_keywords import SPREADSHEET_KEYWORDS, prefers_isolated_browser_for_goal
from src.gateway.transcript import get_transcript_store
from src.cron.scheduler import get_scheduler
from src.runtime.task_store import get_task_store
from src.tools.tasks import create_task_record, update_task_record
from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4GazeboMissionScenarioDesignerError,
    approve_px4_gazebo_mission_scenario_for_bounded_simulation,
    build_px4_gazebo_mission_designer_sitl_execution_request,
    run_px4_gazebo_mission_scenario_designer,
)
from src.runtime.px4_gazebo_mission_designer_sitl_runner import (
    PX4GazeboMissionDesignerSITLRunnerError,
    mission_designer_sitl_execution_opted_in,
    run_px4_gazebo_mission_designer_sitl_execution,
)
from src.runtime.px4_gazebo_mission_designer_sitl_live_flight_run import (
    MISSIONOS_AUTO_MISSION_GUI_DISPATCH_OPT_IN_ENV,
    PX4GazeboMissionDesignerSITLLiveFlightRunError,
    attach_px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt,
    attach_px4_gazebo_mission_designer_sitl_live_flight_failed_receipt,
    mission_designer_live_sitl_flight_opted_in,
    missionos_auto_mission_gui_dispatch_opted_in,
    run_missionos_auto_mission_gui_dispatch_execution,
    run_px4_gazebo_mission_designer_live_sitl_flight_execution,
)
from src.runtime.px4_gazebo_emergency_dispatcher import (
    PX4GazeboEmergencyCommandDispatchStatus,
    PX4GazeboEmergencyDispatcherError,
    build_px4_gazebo_emergency_command_allowlist,
    build_px4_gazebo_emergency_command_approval,
    run_px4_gazebo_emergency_command_dispatch,
)

MISSIONOS_RUNTIME_RECOVERY_EMERGENCY_ACTIONS = {"land", "return_to_launch"}
MISSIONOS_RUNTIME_RECOVERY_MANEUVER_ACTIONS = {
    "adjust_altitude",
    "adjust_speed",
    "reroute",
    "avoid_obstacle",
}
MISSIONOS_RUNTIME_RECOVERY_ACTIONS = (
    MISSIONOS_RUNTIME_RECOVERY_EMERGENCY_ACTIONS
    | MISSIONOS_RUNTIME_RECOVERY_MANEUVER_ACTIONS
)
from src.runtime.px4_gazebo_sitl_execution_readiness import (
    build_px4_gazebo_sitl_execution_readiness,
)
from src.runtime.mission_designer_envelope_violation_advisory import (
    MISSION_DESIGNER_CONTRACT_MAX_PAYLOAD_KG,
    MISSION_DESIGNER_CONTRACT_MAX_WIND_SPEED_MPS,
    build_envelope_violation_advisory,
    envelope_violation_advisory_requested,
)
from src.runtime.missionos_auto_mission_runner import MissionOSAutoMissionRunnerError
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_MISSION_ACCEPTED,
    PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_CONTAINER_ENV,
    PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_OPT_IN_ENV,
    PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_REUSE_CONTAINER_ENV,
)

_HEARTBEAT_INTERVAL = 30  # seconds
_AGENT_TIMEOUT = 120       # seconds
_MAX_PENDING_PER_SESSION = 100
_MAX_PENDING_SESSIONS = 500
_FRESHNESS_KEYWORDS = {
    "最新", "最近", "ニュース", "調べて", "噂", "今年", "来日", "予定",
    "公演", "ライブ", "フェス", "開催", "話題", "リサーチ", "調査",
    "推測", "予測", "見通し", "発表", "gtc", "tour", "festival",
}


def _write_missionos_auto_operator_recovery_request_to_container(
    *,
    container_path: str,
    request_payload: Mapping[str, Any],
    container_name: str | None = None,
) -> dict[str, Any]:
    """Queue an operator-approved recovery request inside the active SITL container.

    The AUTO runtime probe runs inside the PX4/Gazebo container, so a host-side
    artifact write is not enough for an in-flight command. Keep this helper
    deliberately narrow: it only writes the known /tmp request path used by the
    active AUTO runner.
    """

    import scripts.smoke_px4_gazebo_sitl_mission_upload as upload_smoke

    path = str(container_path or "").strip()
    if not path.startswith("/tmp/missionos_auto_operator_recovery_request_"):
        raise ValueError("operator recovery request path must be the AUTO /tmp path")
    if "\x00" in path or "\n" in path or "\r" in path:
        raise ValueError("operator recovery request path contains invalid characters")
    target_container = str(container_name or upload_smoke.CONTAINER_NAME)
    payload_text = json.dumps(dict(request_payload), sort_keys=True)
    upload_smoke._run(
        [
            "docker",
            "exec",
            "-i",
            target_container,
            "sh",
            "-c",
            'cat > "$1.tmp" && mv "$1.tmp" "$1"',
            "sh",
            path,
        ],
        input_text=payload_text,
        timeout=10,
    )
    return {
        "request_status": "queued",
        "container_name": target_container,
        "container_path": path,
        "bytes_written": len(payload_text.encode("utf-8")),
    }


def _bounded_recovery_float(
    parameters: Mapping[str, Any],
    *keys: str,
    minimum: float,
    maximum: float,
) -> float | None:
    for key in keys:
        raw = parameters.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be numeric",
            ) from exc
        if not minimum <= value <= maximum:
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be between {minimum:g} and {maximum:g}",
            )
        return value
    return None


def _bounded_operator_recovery_parameters(
    *,
    recovery_action: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    raw = body.get("recovery_parameters") or body.get("parameters") or {}
    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, Mapping):
        raise HTTPException(
            status_code=400,
            detail="recovery_parameters must be an object",
        )
    parameters = dict(raw)
    if recovery_action in MISSIONOS_RUNTIME_RECOVERY_EMERGENCY_ACTIONS:
        return {}
    if recovery_action == "adjust_altitude":
        altitude = _bounded_recovery_float(
            parameters,
            "target_altitude_m",
            "altitude_m",
            minimum=0.5,
            maximum=500.0,
        )
        if altitude is None:
            raise HTTPException(
                status_code=400,
                detail="adjust_altitude requires target_altitude_m",
            )
        return {"target_altitude_m": altitude}
    if recovery_action == "adjust_speed":
        speed = _bounded_recovery_float(
            parameters,
            "target_speed_mps",
            "speed_mps",
            minimum=0.5,
            maximum=30.0,
        )
        if speed is None:
            raise HTTPException(
                status_code=400,
                detail="adjust_speed requires target_speed_mps",
            )
        return {"target_speed_mps": speed}
    if recovery_action in {"reroute", "avoid_obstacle"}:
        target_x = _bounded_recovery_float(
            parameters,
            "target_x_m",
            "x_m",
            minimum=-5000.0,
            maximum=5000.0,
        )
        target_y = _bounded_recovery_float(
            parameters,
            "target_y_m",
            "y_m",
            minimum=-5000.0,
            maximum=5000.0,
        )
        altitude = _bounded_recovery_float(
            parameters,
            "target_altitude_m",
            "altitude_m",
            minimum=0.5,
            maximum=500.0,
        )
        if target_x is None or target_y is None:
            detail = (
                "avoid_obstacle requires target_x_m and target_y_m from an "
                "obstacle-aware route planner"
                if recovery_action == "avoid_obstacle"
                else "reroute requires target_x_m and target_y_m"
            )
            raise HTTPException(status_code=400, detail=detail)
        out = {"target_x_m": target_x, "target_y_m": target_y}
        if altitude is not None:
            out["target_altitude_m"] = altitude
        if recovery_action == "avoid_obstacle":
            out["obstacle_avoidance_required"] = True
        return out
    return {}


def _operator_recovery_approval_payload(
    *,
    recovery_action: str,
    task_id: str,
    parameters: Mapping[str, Any],
    now: datetime,
) -> tuple[Any, Any]:
    if recovery_action in MISSIONOS_RUNTIME_RECOVERY_EMERGENCY_ACTIONS:
        approval = build_px4_gazebo_emergency_command_approval(
            operator_approval_performed=True,
            approved_recovery_actions=[recovery_action],
            now=now,
            metadata={
                "operator_surface": "missionos_runtime_recovery",
                "task_id": task_id,
                "explicit_recovery_dispatch_approval": True,
            },
        )
        allowlist = build_px4_gazebo_emergency_command_allowlist(
            approval=approval,
            now=now,
            metadata={
                "operator_surface": "missionos_runtime_recovery",
                "task_id": task_id,
            },
        )
        return approval, allowlist
    approval = {
        "schema_version": "missionos_runtime_recovery_maneuver_approval.v1",
        "approval_id": f"runtime_recovery_maneuver_approval_{uuid.uuid4().hex[:12]}",
        "task_id": task_id,
        "operator_approval_performed": True,
        "approved_recovery_action": recovery_action,
        "approved_parameters": dict(parameters),
        "operator_surface": "missionos_runtime_recovery",
        "explicit_recovery_dispatch_approval": True,
        "approval_free_recovery_dispatch_allowed": False,
        "delivery_completion_claimed": False,
        "progress_counted": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "approved_at": now.isoformat(),
    }
    allowlist = {
        "schema_version": "missionos_runtime_recovery_maneuver_allowlist.v1",
        "allowlist_id": f"runtime_recovery_maneuver_allowlist_{uuid.uuid4().hex[:12]}",
        "task_id": task_id,
        "allowed_recovery_actions": [recovery_action],
        "allowed_parameters": dict(parameters),
        "allowed_mavlink_message_ids": (
            ["COMMAND_LONG:MAV_CMD_DO_CHANGE_SPEED"]
            if recovery_action == "adjust_speed"
            else ["SET_POSITION_TARGET_LOCAL_NED", "COMMAND_LONG:MAV_CMD_DO_SET_MODE"]
        ),
        "active_runner_required": True,
        "delivery_completion_claimed": False,
        "progress_counted": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "created_at": now.isoformat(),
    }
    return approval, allowlist


def _approval_json(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _recovery_float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _operator_recovery_proposal_policy() -> dict[str, Any]:
    return {
        "policy_ref": "operator_requested_runtime_recovery_proposal_policy.v1",
        "preauthorized_actions": sorted(MISSIONOS_RUNTIME_RECOVERY_ACTIONS),
        "battery_return_threshold_percent": 20.0,
        "max_route_deviation_xy_m": 100.0,
        "emergency_landing_route_deviation_xy_m": 250.0,
        "min_terrain_clearance_m": 30.0,
        "altitude_adjustment_buffer_m": 5.0,
        "operator_requested_altitude_step_m": 10.0,
        "max_adjust_altitude_m": 500.0,
        "max_adjust_speed_mps": 30.0,
        "max_reroute_target_abs_m": 5000.0,
        "operator_reroute_forward_m": 80.0,
        "operator_reroute_lateral_m": 30.0,
        "obstacle_lateral_clearance_m": 30.0,
        "obstacle_buffer_m": 20.0,
        "obstacle_avoidance_climb_m": 15.0,
    }


def _runtime_recovery_obstacle_from_task_artifacts(
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    route = artifacts.get("mission_designer_coordinate_pair_route")
    route = route if isinstance(route, Mapping) else {}
    obstacle_manifest: Mapping[str, Any] = {}
    obstacle_application: Mapping[str, Any] = {}
    gazebo_obstacle_model_spawned = False
    for key in (
        "missionos_auto_mission_runtime_snapshot",
        "missionos_auto_mission_probe_observed",
        "px4_gazebo_mission_designer_sitl_live_flight_run",
        "missionos_auto_mission_gui_dispatch_receipt",
        "missionos_auto_mission_compilation",
        "obstacle_manifest",
    ):
        payload: Any = artifacts.get(key)
        if key == "obstacle_manifest" and isinstance(payload, Mapping):
            payload = {"obstacle_manifest": payload}
        payload = payload if isinstance(payload, Mapping) else {}
        application = payload.get("gazebo_obstacle_application")
        if isinstance(application, Mapping) and application:
            obstacle_application = application
            if application.get("gazebo_obstacle_model_spawned") is True:
                gazebo_obstacle_model_spawned = True
            app_manifest = application.get("obstacle_manifest")
            if isinstance(app_manifest, Mapping) and app_manifest:
                obstacle_manifest = app_manifest
                if app_manifest.get("gazebo_obstacle_model_spawned") is True:
                    gazebo_obstacle_model_spawned = True
        manifest = payload.get("obstacle_manifest")
        if isinstance(manifest, Mapping) and manifest:
            obstacle_manifest = manifest
            if manifest.get("gazebo_obstacle_model_spawned") is True:
                gazebo_obstacle_model_spawned = True
        if payload.get("gazebo_obstacle_model_spawned") is True:
            gazebo_obstacle_model_spawned = True
    if not obstacle_manifest:
        route_manifest = route.get("obstacle_manifest")
        if isinstance(route_manifest, Mapping) and route_manifest:
            obstacle_manifest = route_manifest
        elif isinstance(route.get("obstacles"), list) and route.get("obstacles"):
            obstacle_manifest = {
                "schema_version": "missionos_gazebo_obstacle_manifest.v1",
                "manifest_status": "configured",
                "source": "mission_designer_coordinate_pair_route",
                "obstacles": list(route.get("obstacles") or []),
                "building_risk_detected": True,
            }
    landing_zone_blocked = route.get("landing_zone_blocked") is True
    detected = bool(obstacle_manifest) or landing_zone_blocked
    return {
        "projection_status": "source_backed" if detected else "not_configured",
        "obstacle_detected": detected,
        "building_risk_detected": bool(
            (obstacle_manifest.get("building_risk_detected") if obstacle_manifest else False)
            or route.get("building_risk_detected")
            or landing_zone_blocked
        ),
        "landing_zone_blocked": landing_zone_blocked,
        "obstacle_manifest": dict(obstacle_manifest),
        "gazebo_obstacle_application": dict(obstacle_application),
        "gazebo_obstacle_model_spawned": gazebo_obstacle_model_spawned,
    }


def _runtime_recovery_telemetry_from_task_artifacts(
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    bridge = artifacts.get("missionos_runtime_recovery_agent_live_bridge")
    bridge = bridge if isinstance(bridge, Mapping) else {}
    telemetry = bridge.get("telemetry_snapshot")
    if isinstance(telemetry, Mapping) and telemetry:
        return dict(telemetry)
    snapshot = artifacts.get("missionos_auto_mission_runtime_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    if not snapshot:
        return {}
    compilation = artifacts.get("missionos_auto_mission_compilation")
    compilation = compilation if isinstance(compilation, Mapping) else {}
    planned_route_m = _recovery_float_or_none(
        compilation.get("planned_route_m") or snapshot.get("planned_route_m")
    )
    progress_m = _recovery_float_or_none(snapshot.get("progress_m"))
    remaining_route_m = None
    if planned_route_m is not None and progress_m is not None:
        remaining_route_m = max(0.0, planned_route_m - progress_m)
    terrain_clearance_m = _recovery_float_or_none(
        snapshot.get("terrain_clearance_m") or snapshot.get("clearance_m")
    )
    terrain_target_m = _recovery_float_or_none(
        snapshot.get("terrain_clearance_target_m")
        or snapshot.get("target_clearance_m")
    )
    terrain_margin_m = _recovery_float_or_none(
        snapshot.get("terrain_clearance_margin_m")
        or snapshot.get("clearance_margin_m")
    )
    if terrain_margin_m is None and terrain_clearance_m is not None:
        terrain_target = terrain_target_m if terrain_target_m is not None else 30.0
        terrain_margin_m = terrain_clearance_m - terrain_target
    return {
        "source": "missionos_auto_mission_runtime_snapshot",
        "sample_index": snapshot.get("sample_index"),
        "elapsed_seconds": snapshot.get("elapsed_seconds"),
        "route": {
            "progress_m": progress_m,
            "planned_route_m": planned_route_m,
            "remaining_route_m": remaining_route_m,
            "deviation_xy_m": snapshot.get("deviation_xy_m")
            or snapshot.get("wind_drift_deviation_xy_m"),
            "mission_current_seq": snapshot.get("mission_current_seq"),
            "mission_reached_seq": snapshot.get("mission_reached_seq"),
            "waypoint_total": snapshot.get("waypoint_total"),
        },
        "terrain": {
            "terrain_clearance_m": terrain_clearance_m,
            "terrain_clearance_target_m": terrain_target_m,
            "terrain_clearance_margin_m": terrain_margin_m,
            "terrain_clearance_below_minimum": (
                snapshot.get("terrain_clearance_below_minimum")
                if "terrain_clearance_below_minimum" in snapshot
                else terrain_margin_m is not None and terrain_margin_m < 0
            ),
        },
        "obstacle": _runtime_recovery_obstacle_from_task_artifacts(artifacts),
        "position": {
            "local_x_m": snapshot.get("local_x_m"),
            "local_y_m": snapshot.get("local_y_m"),
            "local_z_m": snapshot.get("local_z_m"),
            "altitude_above_home_m": snapshot.get("altitude_above_home_m"),
            "distance_to_home_m": snapshot.get("distance_to_home_m"),
        },
        "battery": {
            "remaining_percent": _recovery_float_or_none(
                snapshot.get("battery_remaining_percent")
            ),
            "delta_percent": _recovery_float_or_none(
                snapshot.get("battery_remaining_delta_percent")
            ),
            "warning": snapshot.get("battery_warning"),
        },
        "wind": {
            "speed_mps": _recovery_float_or_none(
                snapshot.get("wind_speed_mps") or snapshot.get("weather_wind_speed_mps")
            ),
        },
        "telemetry": {
            "stale": snapshot.get("heartbeat_observed") is False,
            "dropout": False,
        },
        "nav_state": snapshot.get("nav_state"),
        "arming_state": snapshot.get("arming_state"),
        "landed": snapshot.get("landed"),
    }


def _runtime_recovery_operator_proposal_response(
    *,
    task_id: str,
    operator_instruction: str,
    requested_action: str,
    requested_parameters: Mapping[str, Any],
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    planner_result: Mapping[str, Any],
) -> dict[str, Any]:
    candidate = planner_result.get("recommended_candidate")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    proposed_parameters = candidate.get("proposed_parameters")
    proposed_parameters = proposed_parameters if isinstance(proposed_parameters, Mapping) else {}
    selected_action = str(candidate.get("selected_bounded_action") or "operator_review")
    proposal_status = str(planner_result.get("tool_status") or "insufficient_context")
    guardrail_assessment = planner_result.get("recovery_guardrail_assessment")
    guardrail_assessment = (
        dict(guardrail_assessment) if isinstance(guardrail_assessment, Mapping) else {}
    )
    return {
        "schema_version": "missionos_runtime_recovery_operator_request_proposal.v1",
        "task_id": task_id,
        "operator_instruction": operator_instruction,
        "requested_action": requested_action,
        "requested_parameters": dict(requested_parameters),
        "proposal_status": proposal_status,
        "selected_bounded_action": selected_action,
        "proposed_parameters": dict(proposed_parameters),
        "recommended_candidate": dict(candidate),
        "recovery_planner_tool_result": dict(planner_result),
        "recovery_guardrail_assessment": guardrail_assessment,
        "telemetry_snapshot": dict(telemetry_snapshot),
        "recovery_policy": dict(recovery_policy),
        "dispatch_authority_created": False,
        "operator_approval_required": True,
        "physical_execution_invoked": False,
        "progress_counted": False,
        "summary": {
            "task_id": task_id,
            "proposal_status": proposal_status,
            "selected_bounded_action": selected_action,
            "proposed_parameters": dict(proposed_parameters),
            "guardrail_status": str(
                guardrail_assessment.get("assessment_status")
                or planner_result.get("guardrail_status")
                or ""
            ),
            "blocking_reasons": list(
                guardrail_assessment.get("blocking_reasons") or []
            ),
            "dispatch_authority_created": False,
            "operator_approval_required": True,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
    }


_MISSION_DESIGNER_FULLWIDTH_TRANSLATION = str.maketrans(
    "０１２３４５６７８９．，、",
    "0123456789.,,",
)
_MISSION_DESIGNER_RELATIVE_ROUTE_PATTERN = re.compile(
    r"(?:北|南|東|西|北東|北西|南東|南西|north|south|east|west|northeast|northwest|southeast|southwest)"
    r".{0,24}?"
    r"\d+(?:[.,]\d+)?\s*(?:m|meter|meters|metre|metres|メートル|ｍ)",
    re.IGNORECASE,
)
_MISSION_DESIGNER_COORDINATE_ROUTE_BLOCKED_REASON = "operator_route_not_bound_to_sitl"


def _mission_designer_operator_route_requested(
    *,
    artifacts: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> bool:
    if isinstance(artifacts.get("mission_designer_coordinate_pair_route"), dict):
        return True
    if metadata.get("operator_route_requested") is True:
        return True
    proposal = artifacts.get("px4_gazebo_mission_scenario_proposal")
    proposal = proposal if isinstance(proposal, Mapping) else {}
    prompt = str(proposal.get("mission_objective") or "")
    normalized = prompt.translate(_MISSION_DESIGNER_FULLWIDTH_TRANSLATION)
    return bool(_MISSION_DESIGNER_RELATIVE_ROUTE_PATTERN.search(normalized))


def _mission_designer_route_bound_to_sitl(artifacts: Mapping[str, Any]) -> bool:
    coordinate_binding = artifacts.get("mission_designer_coordinate_pair_sitl_binding")
    coordinate_binding = (
        coordinate_binding if isinstance(coordinate_binding, Mapping) else {}
    )
    if (
        coordinate_binding.get("binding_status")
        == "bound_to_operator_coordinate_route"
        and coordinate_binding.get("sitl_only") is True
        and coordinate_binding.get("hardware_target_allowed") is False
        and coordinate_binding.get("physical_execution_invoked") is False
    ):
        return True
    gate = artifacts.get("digital_twin_sitl_binding_gate")
    gate = gate if isinstance(gate, Mapping) else {}
    return (
        gate.get("binding_allowed") is True
        and gate.get("sitl_execution_bound") is True
        and gate.get("px4_mission_upload_allowed") is True
    )


def _coordinate_pair_route_sitl_mission_items(
    route: Mapping[str, Any],
) -> list[dict[str, Any]]:
    takeoff_lat = float(route["takeoff_latitude"])
    takeoff_lon = float(route["takeoff_longitude"])
    dropoff_lat = float(route["dropoff_latitude"])
    dropoff_lon = float(route["dropoff_longitude"])
    altitude_m = min(
        max(float(route.get("dropoff_roof_height_agl_m") or 0.0), 10.0),
        120.0,
    )
    takeoff_altitude_m = 15.0
    staged_altitude_m = min(max(20.0, altitude_m / 2.0), 120.0)
    final_altitude_m = min(max(altitude_m, staged_altitude_m), 120.0)
    midpoint_lat = round((takeoff_lat + dropoff_lat) / 2.0, 7)
    midpoint_lon = round((takeoff_lon + dropoff_lon) / 2.0, 7)
    return [
        {
            "seq": 0,
            "command": 22,
            "latitude_deg": round(takeoff_lat, 7),
            "longitude_deg": round(takeoff_lon, 7),
            "altitude_m": takeoff_altitude_m,
            "current": 1,
        },
        {
            "seq": 1,
            "command": 16,
            "latitude_deg": midpoint_lat,
            "longitude_deg": midpoint_lon,
            "altitude_m": staged_altitude_m,
        },
        {
            "seq": 2,
            "command": 16,
            "latitude_deg": round(dropoff_lat, 7),
            "longitude_deg": round(dropoff_lon, 7),
            "altitude_m": final_altitude_m,
        },
        {
            "seq": 3,
            "command": 21,
            "latitude_deg": round(dropoff_lat, 7),
            "longitude_deg": round(dropoff_lon, 7),
            "altitude_m": 0.0,
        },
    ]


def _mission_designer_coordinate_pair_sitl_binding(
    *,
    route: Mapping[str, Any],
    scenario_approval: Mapping[str, Any],
    bounded_request: Mapping[str, Any],
) -> dict[str, Any]:
    operator_approved = scenario_approval.get("operator_approved") is True
    approved_for_bounded_simulation = (
        bounded_request.get("approved_for_bounded_simulation") is True
    )
    route_ref = f"mission_designer_coordinate_pair_route:{route.get('route_id')}"
    mission_items = _coordinate_pair_route_sitl_mission_items(route)
    binding_status = (
        "bound_to_operator_coordinate_route"
        if operator_approved and approved_for_bounded_simulation
        else "blocked"
    )
    blocked_reasons = []
    if not operator_approved:
        blocked_reasons.append("operator_approval_missing")
    if not approved_for_bounded_simulation:
        blocked_reasons.append("bounded_simulation_request_not_approved")
    return {
        "schema_version": "mission_designer_coordinate_pair_sitl_binding.v1",
        "binding_status": binding_status,
        "route_ref": route_ref,
        "mission_items_source": "operator_coordinate_pair_route",
        "mission_items": mission_items,
        "mission_item_count": len(mission_items),
        "operator_approved": operator_approved,
        "approved_for_bounded_simulation": approved_for_bounded_simulation,
        "requires_explicit_execution_approval": True,
        "server_opt_in_required": True,
        "sitl_only": True,
        "gazebo_execution_invoked": False,
        "mavlink_dispatch_performed": False,
        "px4_mission_upload_performed": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "blocked_reasons": blocked_reasons,
    }

_BROWSER_TOOL_NAMES = {
    "control_ui_chat_send_message",
    "computer_observe",
    "computer_click",
    "computer_fill",
    "current_tab_info",
    "current_tab_navigate",
    "current_tab_click",
    "current_tab_fill",
    "current_tab_extract_text",
    "browser_navigate",
    "browser_click",
    "browser_fill",
    "browser_press",
    "browser_extract_text",
    "browser_screenshot",
    "host.browser.navigate",
    "host.browser.click",
    "host.browser.fill",
    "host.browser.press",
    "host.browser.extract_text",
    "host.browser.screenshot",
    "host.control_ui_chat.send_message",
    "host.current_tab.info",
    "host.current_tab.navigate",
    "host.current_tab.click",
    "host.current_tab.fill",
    "host.current_tab.extract_text",
}
_BROWSER_INFRA_ERROR_FRAGMENTS = (
    "playwright is not installed",
    "host bridge is not enabled",
    "requires host bridge",
    "host_bridge_enabled is true but host_bridge_url is not set",
    "host bridge tool call failed",
    "host bridge returned empty tool content",
    "host bridge returned non-json tool content",
    "current tab extension bridge",
    "current tab extension relay",
    "current tab extension is not connected",
    "desktop bridge",
    "desktop_bridge_enabled",
)
_USER_BROWSER_REQUIRED_CAPABILITIES = {
    "desktop.view.frontmost_app",
    "desktop.view.windows",
    "desktop.control.focus_window",
    "desktop.ax.find",
    "desktop.control.click",
    "desktop.control.type",
}
_CONTROL_LOOP_FOLLOWUP_MARKERS = (
    "記載して",
    "入力して",
    "転記して",
    "スプレッドシートに",
    "sheetに",
    "spreadsheetに",
)
_CURRENT_BROWSER_CONTROL_BASE_CONSTRAINTS = [
    "Operate only on the currently visible browser/tab/window.",
    "Do not launch a new browser application or open a managed browser for this task.",
    "Start by identifying the frontmost app and matching it to the existing browser window.",
    "If the current browser window cannot be identified or focused, stop and report an explicit error.",
    "Do not mark the task complete after typing alone; submit the action and verify the resulting page content.",
]
_CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT = (
    "Do not open a new browser tab or window unless the user explicitly asked for it."
)
_CURRENT_BROWSER_PRESERVE_USER_TAB_CONSTRAINT = (
    "Preserve the user's current browser tab. If browsing or search requires a "
    "different page, open a new tab in the same browser window. Otherwise stay "
    "on the current tab."
)
_ISOLATED_BROWSER_TEXT_ENTRY_CONSTRAINTS = [
    "For current-browser visible text-entry or form-filling work, use an isolated browser or managed browser page instead of the user's existing browser tabs or forms.",
    "Do not interact with pre-existing browser tabs, windows, or form fields owned by the user.",
    "Verify the final URL/title/content inside that isolated browser session before marking the task complete.",
]


@dataclass
class SpecialistToolFailure:
    tool_name: str
    error: str
    infrastructure: bool = False


@dataclass
class SpecialistPrepassResult:
    text: str = ""
    tool_failures: list[SpecialistToolFailure] = field(default_factory=list)
    used_tools: set[str] = field(default_factory=set)
    evidence_blocks: list[str] = field(default_factory=list)

    @property
    def infrastructure_blocked(self) -> bool:
        return any(item.infrastructure for item in self.tool_failures)

    @property
    def browser_failure(self) -> bool:
        return any(item.tool_name in _BROWSER_TOOL_NAMES for item in self.tool_failures)


class ConnectionManager:
    """WebSocket connection + running task management"""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        # session_id -> user_id mapping for system event routing
        self._session_users: Dict[str, str] = {}
        self._pending_events: Dict[str, list[dict[str, Any]]] = {}

    async def connect(self, websocket: WebSocket, session_id: str, user_id: str = "") -> None:
        await websocket.accept()
        self.active_connections[session_id] = websocket
        if user_id:
            self._session_users[session_id] = user_id

    def disconnect(
        self,
        session_id: str,
        *,
        preserve_pending: bool = False,
        preserve_user: bool = False,
    ) -> None:
        self.active_connections.pop(session_id, None)
        if not preserve_user:
            self._session_users.pop(session_id, None)
        if not preserve_pending:
            self._pending_events.pop(session_id, None)

    def get_user_id(self, session_id: str) -> Optional[str]:
        return self._session_users.get(session_id)

    async def send_json(self, session_id: str, data: dict) -> None:
        ws = self.active_connections.get(session_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def broadcast_json(self, data: dict, exclude: Optional[str] = None) -> None:
        for sid, ws in list(self.active_connections.items()):
            if sid == exclude:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def send_or_queue_json(self, session_id: str, data: dict) -> None:
        if session_id in self.active_connections:
            await self.send_json(session_id, data)
            return
        queue = self._pending_events.setdefault(session_id, [])
        if len(queue) >= _MAX_PENDING_PER_SESSION:
            queue.pop(0)
        queue.append(data)
        if len(self._pending_events) > _MAX_PENDING_SESSIONS:
            oldest_key = next(iter(self._pending_events))
            del self._pending_events[oldest_key]

    async def flush_pending(self, session_id: str) -> None:
        pending = self._pending_events.pop(session_id, [])
        for payload in pending:
            await self.send_json(session_id, payload)

    async def flush_all_pending(self) -> None:
        for session_id in list(self.active_connections.keys()):
            await self.flush_pending(session_id)

    # --- task tracking for abort ---

    def set_task(self, session_id: str, task: asyncio.Task) -> None:
        self._tasks[session_id] = task

    def clear_task(self, session_id: str) -> None:
        self._tasks.pop(session_id, None)

    async def abort(self, session_id: str) -> bool:
        task = self._tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
            return True
        return False


class GatewayServer:
    """Gateway server with typed protocol, transcript, cron platform, and tool security."""

    def __init__(self):
        self.settings = get_settings()
        self.session_backend = describe_session_backend(self.settings)
        self.manager = ConnectionManager()
        self.session_service = create_session_service(self.settings)
        self.memory_service = get_promoted_memory_service()
        self.subagent_manager = get_subagent_manager()
        self.runner = Runner(
            agent=root_agent,
            app_name="boiled-claw",
            session_service=self.session_service,
            memory_service=self.memory_service,
        )
        self.routing_session_service = create_session_service(self.settings)
        self.routing_runner = Runner(
            agent=routing_agent,
            app_name="boiled-claw-router",
            session_service=self.routing_session_service,
            memory_service=self.memory_service,
        )
        self.specialist_runners = {
            agent.name: Runner(
                agent=agent,
                app_name="boiled-claw",
                session_service=self.session_service,
                memory_service=self.memory_service,
            )
            for agent in SUB_AGENTS
        }
        self.control_loop = ControlLoop(
            session_service=self.session_service,
            memory_service=self.memory_service,
        )
        self.audit_logger = get_audit_logger()
        self.task_store = get_task_store()
        self.transcript = get_transcript_store()
        self.tool_policy = get_tool_policy_engine()
        self.control_supervisor = ControlLoopSupervisor(
            run_control_loop_with_task=self._run_control_loop_with_task,
            emit_session_event=self._emit_session_event,
        )
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.app = FastAPI(
            title="boiled-claw Gateway",
            version="0.3.0",
            lifespan=self._lifespan,
        )

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @self.app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            api_key = self.settings.gateway_api_key
            if not api_key:
                return await call_next(request)
            public_prefixes = ("/health", "/protocol")
            if any(request.url.path.startswith(p) for p in public_prefixes) or request.url.path == "/":
                return await call_next(request)
            token = (
                request.headers.get("X-API-Key")
                or request.headers.get("Authorization", "").removeprefix("Bearer ")
                or request.query_params.get("token")
            )
            if token != api_key:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return await call_next(request)

        # subagent -> WS notifier
        async def _subagent_notifier(payload: Dict[str, Any]) -> None:
            session_id = payload.get("requester_session_id")
            if not session_id:
                return
            await self._emit_session_event(
                session_id,
                source="subagent",
                status=payload.get("status", ""),
                message=payload.get("message", ""),
                run_id=payload.get("run_id"),
                task_id=payload.get("task_id"),
                agent_name=payload.get("agent_name"),
            )

        self._subagent_notifier_fn = _subagent_notifier

        # cron -> WS/session notifier
        async def _cron_notifier(payload: Dict[str, Any]) -> None:
            event = ev_cron_update(
                job_id=payload.get("job_id", ""),
                status=payload.get("status", ""),
                message=payload.get("message", ""),
            )
            await self.manager.broadcast_json(event)
            requester_session_id = payload.get("requester_session_id")
            if requester_session_id and self.transcript.has_session(requester_session_id):
                await self._emit_session_event(
                    requester_session_id,
                    source="cron",
                    status=payload.get("status", ""),
                    message=payload.get("message", ""),
                )

        self._cron_notifier_fn = _cron_notifier

        async def _approval_notifier(payload: Dict[str, Any]) -> None:
            session_id = payload.get("session_id", "")
            if not session_id:
                return
            approval_payload = {key: value for key, value in payload.items() if key != "event_type"}
            approval_event = str(payload.get("event_type") or "updated")
            await self.manager.send_or_queue_json(
                session_id,
                ev_tools_approval_update(
                    approval_payload,
                    approval_event=approval_event,
                ),
            )
            if approval_payload.get("state") != "pending":
                return
            await self.manager.send_or_queue_json(
                session_id,
                ev_tools_approval_request(
                    request_id=approval_payload.get("request_id", ""),
                    tool_name=approval_payload.get("tool_name", ""),
                    agent_name=approval_payload.get("agent_name", ""),
                    args=approval_payload.get("args") or {},
                    reason=approval_payload.get("reason", ""),
                    state=approval_payload.get("state", "pending"),
                    scope=approval_payload.get("scope", "single"),
                    tool_pattern=approval_payload.get("tool_pattern"),
                    path_scope=approval_payload.get("path_scope"),
                    expires_at=approval_payload.get("expires_at"),
                    propagate_to_subagents=bool(approval_payload.get("propagate_to_subagents", False)),
                    source_request_id=approval_payload.get("source_request_id"),
                ),
            )

        self._approval_notifier_fn = _approval_notifier

        async def _task_notifier(payload: Dict[str, Any]) -> None:
            task = payload.get("task")
            task = task if isinstance(task, dict) else {}
            owner_session_id = str(task.get("owner_session_id") or "")
            if not owner_session_id:
                return
            await self.manager.send_or_queue_json(
                owner_session_id,
                ev_task_update(task, payload.get("event") or {}),
            )

        self._task_notifier_fn = _task_notifier

        def _iter_audit_push_sessions(payload: Dict[str, Any]) -> list[str]:
            metadata = payload.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            primary_session_id = str(payload.get("session_id") or "").strip()
            target_session_id = str(metadata.get("target_session_id") or "").strip()

            session_ids = {primary_session_id} if primary_session_id else set()
            # Only approval resolution events are allowed to fan out to an
            # explicitly-targeted session. Other audit types stay session-local.
            if (
                target_session_id
                and target_session_id != primary_session_id
                and str(payload.get("event_type") or "") == AuditEventType.TOOL_APPROVAL.value
            ):
                session_ids.add(target_session_id)
            return sorted(session_ids)

        async def _audit_notifier(payload: Dict[str, Any]) -> None:
            for session_id in _iter_audit_push_sessions(payload):
                if session_id not in self.manager.active_connections:
                    continue
                await self.manager.send_json(session_id, ev_audit_append(payload))

        self._audit_notifier_fn = _audit_notifier
        self._setup_routes()

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        await self._startup_gateway()
        try:
            yield
        finally:
            await self._shutdown_gateway()

    async def _startup_gateway(self) -> None:
        await ensure_skills_loaded()
        set_subagent_notifier(self._subagent_notifier_fn)
        set_tool_event_notifier(self._send_tool_event)
        self.tool_policy.set_notifier(self._approval_notifier_fn)
        self.task_store.set_notifier(self._task_notifier_fn)
        self.audit_logger.set_notifier(self._audit_notifier_fn)
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="heartbeat"
            )
        scheduler = get_scheduler()
        scheduler.set_spawn_fn(self._spawn_cron_target)
        scheduler.set_notifier(self._cron_notifier_fn)
        scheduler.start()
        await scheduler.fire_system_event("startup")
        running_supervisors = self._running_control_supervisor_tasks()
        self.control_supervisor.watchdog_running_supervisors(running_supervisors)
        await self.control_supervisor.resume_open_supervisors(running_supervisors)

    def _running_control_supervisor_tasks(
        self,
        *,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        page = 1
        tasks: list[dict[str, Any]] = []
        seen_task_ids: set[str] = set()
        while True:
            result = self.task_store.query(
                kind="control_supervisor",
                status="running",
                page=page,
                page_size=page_size,
            )
            for task in result.get("tasks") or []:
                task_id = str(task.get("task_id") or "").strip()
                if not task_id or task_id in seen_task_ids:
                    continue
                seen_task_ids.add(task_id)
                tasks.append(task)
            pagination = (
                result.get("pagination")
                if isinstance(result.get("pagination"), dict)
                else {}
            )
            if not pagination.get("has_more"):
                break
            page += 1
        return tasks

    async def _shutdown_gateway(self) -> None:
        await self.control_supervisor.shutdown()
        set_subagent_notifier(None)
        set_tool_event_notifier(None)
        self.tool_policy.set_notifier(None)
        self.task_store.set_notifier(None)
        self.audit_logger.set_notifier(None)
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None
        await get_scheduler().shutdown()

    def _should_force_web_research(self, message: str) -> bool:
        normalized = (message or "").strip().lower()
        if not normalized or is_direct_stock_price_query(message):
            return False
        return any(keyword in normalized for keyword in _FRESHNESS_KEYWORDS)

    def _select_web_search_timelimit(self, message: str) -> str:
        normalized = (message or "").strip().lower()
        if any(keyword in normalized for keyword in {"今日", "きょう", "today", "速報"}):
            return "d"
        if any(keyword in normalized for keyword in {"今年", "来日", "公演", "ライブ", "フェス", "予定", "開催"}):
            return "y"
        if any(keyword in normalized for keyword in {"最新", "最近", "ニュース", "噂", "発表", "gtc"}):
            return "w"
        return "m"

    async def _send_tool_event(
        self,
        session_id: str,
        payload: dict[str, Any],
    ) -> None:
        if session_id not in self.manager.active_connections:
            return
        await self.manager.send_or_queue_json(session_id, payload)

    def _summarize_tool_result(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            summarized: dict[str, Any] = {}
            for key, value in payload.items():
                if key in {"results"} and isinstance(value, list):
                    summarized[key] = value[:3]
                    summarized["count"] = len(value)
                    continue
                if isinstance(value, str):
                    summarized[key] = value[:400]
                elif isinstance(value, list):
                    summarized[key] = value[:5]
                elif isinstance(value, dict):
                    summarized[key] = self._summarize_tool_result(value)
                else:
                    summarized[key] = value
            return summarized
        return {"value": str(payload)[:400]}

    @staticmethod
    def _tool_response_error(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        error = str(response.get("error") or "").strip()
        if error:
            return error
        if response.get("success") is False:
            return "tool reported success=false"
        if response.get("ok") is False:
            return "tool reported ok=false"
        return None

    @staticmethod
    def _is_browser_infrastructure_error(tool_name: str, error: str) -> bool:
        if tool_name not in _BROWSER_TOOL_NAMES:
            return False
        normalized = (error or "").strip().lower()
        return any(fragment in normalized for fragment in _BROWSER_INFRA_ERROR_FRAGMENTS)

    def _format_specialist_runtime_failure(
        self,
        specialist_name: str,
        result: SpecialistPrepassResult,
    ) -> str:
        first_error = next(
            (item.error for item in result.tool_failures if item.error),
            "required runtime is unavailable",
        )
        if specialist_name in {"browser_automator", "control_ui_chat_operator", "current_tab_operator"} and result.infrastructure_blocked:
            if specialist_name == "current_tab_operator":
                return (
                    "The current browser/tab operation could not run.\n"
                    f"- Cause: {first_error}\n"
                    "- This request will not automatically fall back to desktop control or a managed browser.\n"
                    "- Action: start the Host Bridge and load the Current Tab Adapter extension in Chrome."
                )
            return (
                "The browser operation could not run.\n"
                f"- Cause: {first_error}\n"
                "- This request will not automatically fall back to web_search without operating the browser.\n"
                "- Action: enable the Host Bridge and run Playwright on the host, "
                "or install Playwright in this execution environment."
            )

        if specialist_name == "computer_operator" and result.infrastructure_blocked:
            return (
                "Computer use could not run.\n"
                f"- Cause: {first_error}\n"
                "- This request requires operating the visible browser or GUI.\n"
                "- Action: start the required Host Bridge / Current Tab relay / Desktop Bridge runtime "
                "and make the current browser or target GUI operable on the host."
            )

        return (
            f"{specialist_name} execution failed.\n"
            f"- Cause: {first_error}"
        )

    async def _current_browser_runtime_error(
        self,
        message: str,
    ) -> str | None:
        if prefers_isolated_browser_for_goal(message):
            return None
        if not targets_user_browser(message):
            return None

        if not getattr(self.settings, "desktop_bridge_enabled", False):
            return (
                "MissionOS could not operate the currently open browser or existing spreadsheet.\n"
                "- Cause: Desktop Bridge is disabled.\n"
                "- This request must target the browser you already have open, not a managed browser or local CSV.\n"
                "- Action: set DESKTOP_BRIDGE_ENABLED=true and DESKTOP_BRIDGE_URL, "
                "then start Desktop Bridge on the host."
            )

        try:
            from src.bridges.desktop_bridge_client import get_desktop_client

            client = get_desktop_client()
            capability_result = await client.capabilities()
        except Exception as exc:
            return (
                "MissionOS could not operate the currently open browser or existing spreadsheet.\n"
                f"- Cause: Desktop Bridge capability check failed: {exc}\n"
                "- Action: start Desktop Bridge and confirm the host runtime responds normally."
            )

        implemented = {
            capability.name
            for capability in capability_result.capabilities
            if capability.implemented
        }
        missing = sorted(_USER_BROWSER_REQUIRED_CAPABILITIES - implemented)
        if not missing:
            return None

        available = ", ".join(sorted(implemented)) or "(none)"
        required = ", ".join(sorted(_USER_BROWSER_REQUIRED_CAPABILITIES))
        missing_text = ", ".join(missing)
        return (
            "MissionOS could not operate the currently open browser or existing spreadsheet.\n"
            f"- Cause: Desktop Bridge is missing required capabilities: {missing_text}\n"
            f"- Required capabilities: {required}\n"
            f"- Available capabilities: {available}\n"
            "- This request must target the current browser rather than being replaced with a managed browser or local CSV."
        )

    async def _emit_runner_tool_events(
        self,
        session_id: str,
        event: Event,
        *,
        fallback_request_id: str | None = None,
    ) -> None:
        for function_call in event.get_function_calls():
            await self._send_tool_event(
                session_id,
                ev_tool_start(
                    tool_name=function_call.name or "unknown_tool",
                    agent_name=event.author,
                    args=function_call.args or {},
                    request_id=function_call.id or fallback_request_id,
                ),
            )
        for function_response in event.get_function_responses():
            response = function_response.response or {}
            await self._send_tool_event(
                session_id,
                ev_tool_result(
                    tool_name=function_response.name or "unknown_tool",
                    agent_name=event.author,
                    ok="error" not in response,
                    result=self._summarize_tool_result(response),
                    request_id=function_response.id or fallback_request_id,
                ),
            )

    def _format_web_grounding(self, query: str, result: dict[str, Any]) -> str:
        lines = [f"web_search query: {query}"]
        meta = result.get("meta") or {}
        if meta:
            lines.append(
                f"timelimit={meta.get('timelimit', '')} region={meta.get('region', '')}"
            )
        entries = result.get("results") or []
        if not entries:
            lines.append(
                f"No results. message={result.get('message', 'no search results returned')}"
            )
            return "\n".join(lines)
        for index, item in enumerate(entries[:5], start=1):
            lines.append(f"{index}. {item.get('title', '')}")
            lines.append(f"   URL: {item.get('url', '')}")
            snippet = (item.get("snippet") or "").strip()
            if snippet:
                lines.append(f"   Snippet: {snippet}")
        return "\n".join(lines)

    @staticmethod
    def _extract_grounding_block(message: str) -> str:
        marker = "[Grounding from web_search]\n"
        if marker not in message:
            return ""
        tail = message.split(marker, 1)[1]
        footer = (
            "\n\nUse the web_search grounding above as the primary evidence. "
            "If it is insufficient or contradictory, say so explicitly and avoid guessing."
        )
        if footer in tail:
            tail = tail.split(footer, 1)[0]
        return tail.strip()

    async def _compose_grounded_agent_message(
        self,
        session_id: str,
        user_id: str,
        message: str,
        *,
        research_message: str | None = None,
        agent_name: str = "",
        request_id: str | None = None,
        emit_tool_events: bool = False,
        allow_forced_research: bool = True,
    ) -> str:
        composed = self._compose_agent_message(session_id, message)
        search_query = research_message or message
        if not allow_forced_research or not self._should_force_web_research(search_query):
            return composed

        timelimit = self._select_web_search_timelimit(search_query)
        request_key = request_id or f"grounding:{session_id}"
        resolved_agent_name = agent_name or root_agent.name
        if emit_tool_events:
            await self._send_tool_event(
                session_id,
                ev_tool_start(
                    tool_name="web_search",
                    agent_name=resolved_agent_name,
                    args={
                        "query": search_query,
                        "timelimit": timelimit,
                        "region": "jp-jp",
                    },
                    request_id=request_key,
                ),
            )

        result = await web_search(
            query=search_query,
            timelimit=timelimit,
            region="jp-jp",
        )
        self.audit_logger.log(
            event_type=AuditEventType.WEB_SEARCH,
            user_id=user_id,
            session_id=session_id,
            action="search",
            resource=search_query,
            result="success" if result.get("results") else "empty",
            metadata={
                "timelimit": timelimit,
                "count": len(result.get("results") or []),
                "message": result.get("message", ""),
            },
        )
        if emit_tool_events:
            await self._send_tool_event(
                session_id,
                ev_tool_result(
                    tool_name="web_search",
                    agent_name=resolved_agent_name,
                    ok="error" not in result,
                    result=self._summarize_tool_result(result),
                    request_id=request_key,
                ),
            )

        grounding = self._format_web_grounding(search_query, result)
        return (
            f"{composed}\n\n"
            "[Grounding from web_search]\n"
            f"{grounding}\n\n"
            "Use the web_search grounding above as the primary evidence. "
            "If it is insufficient or contradictory, say so explicitly and avoid guessing."
        )

    async def _emit_routing_event(
        self,
        session_id: str,
        *,
        status: str,
        message: str,
        user_id: str,
        agent_name: str | None = None,
    ) -> None:
        await self._emit_session_event(
            session_id,
            source="router",
            status=status,
            message=message,
            user_id=user_id,
            agent_name=agent_name,
        )

    def _format_root_routing_message(
        self,
        original_message: str,
        decision: RoutingDecision,
        specialist_output: str | None = None,
        specialist_evidence: list[str] | None = None,
    ) -> str:
        lines = [
            "[Gateway routing]",
            f"Primary specialist: {decision.specialist or 'root_agent'}",
        ]
        if decision.reason:
            lines.append(f"Reason: {decision.reason}")
        lines.append(
            "You are still the root_agent. Use the routing context below to decide delegation and synthesis."
        )
        if specialist_evidence:
            lines.append(
                "If specialist evidence is provided, treat it as the primary factual source "
                "for this reply and restate the concrete facts in your answer."
            )
        lines.append(
            "Do not imply that forecast details were already shared unless you actually include "
            "those details in this response."
        )
        if specialist_output:
            lines.extend(
                [
                    "",
                    f"[Specialist output from {decision.specialist}]",
                    specialist_output.strip(),
                ]
            )
        for evidence in specialist_evidence or []:
            lines.extend(
                [
                    "",
                    f"[Specialist evidence from {decision.specialist}]",
                    evidence.strip(),
                ]
            )
        lines.extend(["", "[Original user request]", original_message])
        return "\n".join(lines)

    async def _run_specialist_prepass(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        specialist_name: str,
        request_id: str | None = None,
    ) -> SpecialistPrepassResult:
        runner = self.specialist_runners.get(specialist_name)
        if runner is None:
            return SpecialistPrepassResult()

        full_message = message
        if specialist_name == "web_researcher":
            full_message = await self._compose_grounded_agent_message(
                session_id,
                user_id,
                message,
                research_message=message,
                agent_name=specialist_name,
                request_id=request_id,
                emit_tool_events=True,
                allow_forced_research=True,
            )
        content = types.Content(role="user", parts=[types.Part(text=full_message)])
        partial = ""
        result = SpecialistPrepassResult()
        if specialist_name == "web_researcher":
            grounding = self._extract_grounding_block(full_message)
            if grounding:
                result.evidence_blocks.append(grounding)
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            await self._emit_runner_tool_events(
                session_id,
                event,
                fallback_request_id=request_id,
            )
            for function_call in event.get_function_calls():
                if function_call.name:
                    result.used_tools.add(function_call.name)
            for function_response in event.get_function_responses():
                if function_response.name:
                    result.used_tools.add(function_response.name)
                if function_response.name == "web_search":
                    response = function_response.response or {}
                    query = str(response.get("query") or message).strip()
                    grounding = self._format_web_grounding(query, response)
                    if grounding and grounding not in result.evidence_blocks:
                        result.evidence_blocks.append(grounding)
                error = self._tool_response_error(function_response.response or {})
                if not error:
                    continue
                result.tool_failures.append(
                    SpecialistToolFailure(
                        tool_name=function_response.name or "unknown_tool",
                        error=error,
                        infrastructure=self._is_browser_infrastructure_error(
                            function_response.name or "",
                            error,
                        ),
                    )
                )
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        partial += part.text
        result.text = partial.strip()
        return result

    def _routing_history_block(self, session_id: str, limit: int = 8) -> str:
        lines: list[str] = []
        for entry in self.transcript.get_history(session_id, limit=limit):
            if entry.role not in {"user", "assistant", "inject", "system"}:
                continue
            content = (entry.content or "").strip()
            if not content:
                continue
            lines.append(f"{entry.role}: {content[:280]}")
        return "\n".join(lines) if lines else "(empty)"

    def _build_routing_request(
        self,
        *,
        session_id: str,
        source: str,
        message: str,
        explicit_target: str | None = None,
    ) -> str:
        override = explicit_target or "auto"
        history_block = self._routing_history_block(session_id)
        return (
            f"source={source}\n"
            f"explicit_target={override}\n\n"
            "[Recent transcript]\n"
            f"{history_block}\n\n"
            "[Current request]\n"
            f"{message}\n"
        )

    @staticmethod
    def _extract_json_payload(text: str) -> dict[str, Any] | None:
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(raw[start : end + 1])
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None

    async def _select_route_for_message(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        source: str,
        explicit_target: str | None = None,
    ) -> RoutingDecision:
        prompt = self._build_routing_request(
            session_id=session_id,
            source=source,
            message=message,
            explicit_target=explicit_target,
        )
        routing_session = await self.routing_session_service.create_session(
            app_name="boiled-claw-router",
            user_id=user_id,
            session_id=f"route_{uuid.uuid4().hex[:12]}",
        )
        content = types.Content(role="user", parts=[types.Part(text=prompt)])
        raw_response = ""

        try:
            async with asyncio.timeout(20):
                async for event in self.routing_runner.run_async(
                    user_id=user_id,
                    session_id=routing_session.id,
                    new_message=content,
                ):
                    if event.is_final_response() and event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                raw_response += part.text

            payload = self._extract_json_payload(raw_response)
            if payload is None:
                raise ValueError("routing_agent returned non-JSON output")

            decision = decision_from_payload(payload, fallback_message=message)
            if decision.confidence < 0.35:
                raise ValueError("routing_agent confidence too low")
            return decision
        except Exception as exc:
            fallback = heuristic_decision(message)
            self.audit_logger.log(
                event_type=AuditEventType.AGENT_MESSAGE,
                user_id=user_id,
                session_id=session_id,
                action="routing_fallback",
                resource="routing_agent",
                result="fallback",
                metadata={
                    "error": str(exc),
                    "fallback_target": fallback.route_label,
                },
            )
            return fallback

    @staticmethod
    def _default_dynamic_instruction(message: str) -> str:
        return (
            "You are a dedicated dynamic agent created for a single user task.\n"
            "Work only on the assigned task, stay within the provided tools, and "
            "return concise status and results.\n\n"
            f"Assigned task:\n{message}"
        )

    async def _spawn_dynamic_route(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        decision: RoutingDecision,
    ) -> dict[str, Any]:
        dynamic_request = decision.dynamic_agent
        instruction = (
            dynamic_request.instruction.strip()
            or self._default_dynamic_instruction(message)
        )
        result = await self.subagent_manager.spawn_dynamic(
            task=message,
            instruction=instruction,
            mcp_servers=dynamic_request.mcp_servers,
            requester_session_id=session_id,
            user_id=user_id,
            app_name="boiled-claw",
            mode=dynamic_request.mode or "run",
        )
        return result

    async def _spawn_cron_target(
        self,
        *,
        task: str,
        agent_name: str,
        requester_session_id: str,
        user_id: str,
        app_name: str,
        mode: str = "run",
    ) -> dict[str, Any]:
        if agent_name != "auto":
            return await self.subagent_manager.spawn(
                task=task,
                agent_name=agent_name,
                requester_session_id=requester_session_id,
                user_id=user_id,
                app_name=app_name,
                mode=mode,
            )

        decision = await self._select_route_for_message(
            session_id=requester_session_id,
            user_id=user_id,
            message=task,
            source="cron",
            explicit_target="auto",
        )

        if decision.target == "specialist" and decision.specialist:
            return await self.subagent_manager.spawn(
                task=task,
                agent_name=decision.specialist,
                requester_session_id=requester_session_id,
                user_id=user_id,
                app_name=app_name,
                mode=mode,
            )

        if decision.target == "dynamic_agent":
            return await self._spawn_dynamic_route(
                session_id=requester_session_id,
                user_id=user_id,
                message=task,
                decision=decision,
            )

        run_id = f"cronrt_{uuid.uuid4().hex[:12]}"
        if decision.target == "control_loop":
            asyncio.create_task(
                self._cron_control_loop_task(
                    run_id=run_id,
                    session_id=requester_session_id,
                    user_id=user_id,
                    goal=task,
                ),
                name=f"cron-control:{run_id}",
            )
            return {
                "status": "accepted",
                "run_id": run_id,
                "agent_name": "control_loop",
                "mode": mode,
                "requester_session_id": requester_session_id,
            }

        asyncio.create_task(
            self._cron_root_agent_task(
                run_id=run_id,
                session_id=requester_session_id,
                user_id=user_id,
                message=task,
            ),
            name=f"cron-root:{run_id}",
        )
        return {
            "status": "accepted",
            "run_id": run_id,
            "agent_name": "root_agent",
            "mode": mode,
            "requester_session_id": requester_session_id,
        }

    async def _cron_root_agent_task(
        self,
        *,
        run_id: str,
        session_id: str,
        user_id: str,
        message: str,
    ) -> None:
        result = await self._run_agent_http(user_id, session_id, message)
        await self._deliver_background_result(
            session_id=session_id,
            user_id=user_id,
            source="cron",
            run_id=run_id,
            agent_name="root_agent",
            message=result.get("message", ""),
            ok=bool(result.get("ok")),
            metadata={"type": result.get("type", "agent_message")},
        )

    async def _cron_control_loop_task(
        self,
        *,
        run_id: str,
        session_id: str,
        user_id: str,
        goal: str,
    ) -> None:
        result = await self._run_control_loop_http(
            user_id=user_id,
            session_id=session_id,
            goal=goal,
            constraints=[],
            source="cron",
        )
        await self._deliver_background_result(
            session_id=session_id,
            user_id=user_id,
            source="cron",
            run_id=run_id,
            agent_name="control_loop",
            message=result.final_text,
            ok=result.success,
            metadata={
                "type": "control_loop",
                "plan_id": result.plan_id,
                "task_id": result.metadata.get("task_id"),
            },
        )

    async def _deliver_background_result(
        self,
        *,
        session_id: str,
        user_id: str,
        source: str,
        run_id: str,
        agent_name: str,
        message: str,
        ok: bool,
        metadata: dict[str, Any],
    ) -> None:
        session = self.transcript.get_session(session_id)
        owner_id = session.user_id if session is not None else user_id
        if session is not None and message.strip():
            self.transcript.append(
                session_id,
                "assistant",
                message,
                user_id=owner_id,
                metadata=metadata,
            )
        await self._emit_session_event(
            session_id,
            source=source,
            status="completed" if ok else "failed",
            message=message,
            user_id=owner_id,
            run_id=run_id,
            agent_name=agent_name,
        )

    def _related_approvals_for_task(
        self,
        *,
        approval_ids: list[str],
        session_id: Optional[str],
    ) -> list[dict[str, Any]]:
        if not approval_ids:
            return []
        lookup = {item for item in approval_ids if item}
        approvals = self.tool_policy.list_approvals(
            session_id=session_id,
            state="all",
            include_expired=True,
            limit=max(100, len(lookup) * 4),
        )
        related: list[dict[str, Any]] = []
        seen: set[str] = set()
        for approval in approvals:
            request_id = str(approval.get("request_id") or "")
            source_request_id = str(approval.get("source_request_id") or "")
            if request_id not in lookup and source_request_id not in lookup:
                continue
            if request_id in seen:
                continue
            seen.add(request_id)
            related.append(approval)
        return related

    @staticmethod
    def _approval_is_desktop_tool(tool_name: str) -> bool:
        return str(tool_name or "").startswith("desktop_")

    @staticmethod
    def _approval_family_pattern(tool_name: str) -> str:
        normalized = str(tool_name or "").strip()
        if normalized.startswith("desktop_ax_"):
            return "desktop_ax_*"
        if normalized.startswith("desktop_view_"):
            return "desktop_view_*"
        if normalized.startswith("desktop_wait_"):
            return "desktop_wait_*"
        if normalized.startswith("desktop_control_"):
            return "desktop_control_*"
        return normalized

    @staticmethod
    def _approval_family_label(tool_name: str) -> str:
        pattern = GatewayServer._approval_family_pattern(tool_name)
        labels = {
            "desktop_ax_*": "Desktop AX Family",
            "desktop_view_*": "Desktop View Family",
            "desktop_wait_*": "Desktop Wait Family",
            "desktop_control_*": "Desktop Control Family",
        }
        return labels.get(pattern, str(tool_name or "Tool"))

    def _session_pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id:
            return []
        result = self.tool_policy.query_approvals(
            session_id=session_id,
            state="pending",
            include_expired=False,
            page=1,
            page_size=200,
        )
        approvals = result.get("approvals")
        return approvals if isinstance(approvals, list) else []

    def _approval_resolve_suggestions(self, approval: dict[str, Any]) -> list[dict[str, Any]]:
        if str(approval.get("state") or "") not in {"pending", "expiring"}:
            return []
        session_id = str(approval.get("session_id") or "")
        tool_name = str(approval.get("tool_name") or approval.get("tool_pattern") or "")
        if not session_id or not tool_name:
            return []
        pending = self._session_pending_approvals(session_id)
        family_pattern = self._approval_family_pattern(tool_name)
        desktop_pending = [
            item for item in pending if self._approval_is_desktop_tool(item.get("tool_name") or "")
        ]
        suggestions = [
            {
                "strategy": "session_exact",
                "label": "Approve This Tool For Session",
                "description": "Reuse this exact tool approval for later requests in the same session.",
                "affected_count": max(
                    1,
                    sum(
                        1
                        for item in pending
                        if str(item.get("tool_name") or "") == tool_name
                    ),
                ),
                "tool_pattern": tool_name,
                "scope": "session",
            }
        ]
        if family_pattern and family_pattern != tool_name:
            suggestions.append(
                {
                    "strategy": "family_session",
                    "label": f"Approve {self._approval_family_label(tool_name)}",
                    "description": "Reuse a family-scoped approval for similar desktop capabilities in this session.",
                    "affected_count": max(
                        1,
                        sum(
                            1
                            for item in pending
                            if self._approval_family_pattern(item.get("tool_name") or "") == family_pattern
                        ),
                    ),
                    "tool_pattern": family_pattern,
                    "scope": "session",
                }
            )
        if self._approval_is_desktop_tool(tool_name) and desktop_pending:
            suggestions.append(
                {
                    "strategy": "desktop_session_pack",
                    "label": "Approve Desktop Pack For Session",
                    "description": "Resolve all currently-pending desktop approvals in this session using family-scoped rules.",
                    "affected_count": len(desktop_pending),
                    "tool_pattern": "desktop::*",
                    "scope": "session",
                }
            )
        return suggestions

    def _approval_bundle_specs(
        self,
        approval: dict[str, Any],
        *,
        strategy: str,
        path_scope: Optional[str] = None,
        propagate_to_subagents: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        request_id = str(approval.get("request_id") or "")
        session_id = str(approval.get("session_id") or "")
        tool_name = str(approval.get("tool_name") or approval.get("tool_pattern") or "")
        if not request_id or not session_id or not tool_name:
            return []

        pending = self._session_pending_approvals(session_id)
        family_pattern = self._approval_family_pattern(tool_name)

        def build_item(item: dict[str, Any], tool_pattern_value: str) -> dict[str, Any]:
            return {
                "request_id": str(item.get("request_id") or ""),
                "scope": "session",
                "tool_pattern": tool_pattern_value,
                "path_scope": path_scope if path_scope is not None else item.get("path_scope"),
                "propagate_to_subagents": (
                    bool(propagate_to_subagents)
                    if propagate_to_subagents is not None
                    else bool(item.get("propagate_to_subagents"))
                ),
            }

        if strategy == "single":
            return [{"request_id": request_id, "scope": approval.get("scope") or "single"}]
        if strategy == "session_exact":
            return [
                build_item(item, tool_name)
                for item in pending
                if str(item.get("tool_name") or "") == tool_name
            ] or [build_item(approval, tool_name)]
        if strategy == "family_session":
            return [
                build_item(item, self._approval_family_pattern(item.get("tool_name") or ""))
                for item in pending
                if self._approval_family_pattern(item.get("tool_name") or "") == family_pattern
            ] or [build_item(approval, family_pattern)]
        if strategy == "desktop_session_pack":
            if not self._approval_is_desktop_tool(tool_name):
                raise ValueError("desktop_session_pack is only available for desktop approvals")
            return [
                build_item(item, self._approval_family_pattern(item.get("tool_name") or ""))
                for item in pending
                if self._approval_is_desktop_tool(item.get("tool_name") or "")
            ] or [build_item(approval, family_pattern)]
        raise ValueError(f"unsupported approval bundle strategy: {strategy}")

    def _control_loop_seed_payload(
        self,
        *,
        goal: str,
        constraints: list[str],
        source: str,
        request_id: Optional[str],
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        replay_from_step: Optional[str] = None,
        replay_mode: Optional[str] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        artifacts: dict[str, Any] = {
            "goal": goal,
            "constraints": constraints,
            "resume_context": {
                "goal": goal,
                "constraints": constraints,
            },
        }
        metadata: dict[str, Any] = {
            "source": source,
            "request_id": request_id,
        }
        if replay_of_task_id:
            artifacts["replay"] = {
                "source_task_id": replay_of_task_id,
                "compare_to_task_id": compare_to_task_id or replay_of_task_id,
            }
            artifacts["resume_context"]["replay_of_task_id"] = replay_of_task_id
            metadata["replay_of_task_id"] = replay_of_task_id
            metadata["compare_to_task_id"] = compare_to_task_id or replay_of_task_id
            if replay_from_step:
                artifacts["replay"]["from_step"] = replay_from_step
                artifacts["resume_context"]["replay_from_step"] = replay_from_step
                metadata["replay_from_step"] = replay_from_step
            if replay_mode:
                artifacts["replay"]["mode"] = replay_mode
                artifacts["resume_context"]["replay_mode"] = replay_mode
                metadata["replay_mode"] = replay_mode
        return artifacts, metadata

    def _create_control_loop_task_record(
        self,
        *,
        user_id: str,
        session_id: str,
        owner_session_id: Optional[str] = None,
        goal: str,
        constraints: list[str],
        request_id: Optional[str],
        source: str,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        replay_from_step: Optional[str] = None,
        replay_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        artifacts, metadata = self._control_loop_seed_payload(
            goal=goal,
            constraints=constraints,
            source=source,
            request_id=request_id,
            replay_of_task_id=replay_of_task_id,
            compare_to_task_id=compare_to_task_id,
            replay_from_step=replay_from_step,
            replay_mode=replay_mode,
        )
        return create_task_record(
            kind="control_loop",
            title=goal,
            status="running",
            owner_session_id=owner_session_id or session_id,
            owner_user_id=user_id,
            parent_task_id=parent_task_id,
            artifacts=artifacts,
            metadata=metadata,
        )

    def _find_control_loop_task_for_approval(
        self,
        *,
        session_id: str,
        request_id: str,
    ) -> str | None:
        task = self._find_control_loop_task_record_for_approval(
            session_id=session_id,
            request_id=request_id,
        )
        if not isinstance(task, dict):
            return None
        task_id = str(task.get("task_id") or "").strip()
        return task_id or None

    def _find_control_loop_task_owner_for_approval(
        self,
        *,
        session_id: str,
        request_id: str,
    ) -> str | None:
        task = self._find_control_loop_task_record_for_approval(
            session_id=session_id,
            request_id=request_id,
        )
        if not isinstance(task, dict):
            return None
        owner_user_id = str(task.get("owner_user_id") or "").strip()
        return owner_user_id or None

    def _find_control_loop_task_record_for_approval(
        self,
        *,
        session_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        request_id = str(request_id or "").strip()
        if not session_id or not request_id:
            return None
        payload = self.task_store.query(
            owner_session_id=session_id,
            kind="control_loop",
            status="open",
            page=1,
            page_size=100,
        )
        for task in payload.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            artifacts = task.get("artifacts") or {}
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            result = artifacts.get("result") or {}
            result = result if isinstance(result, dict) else {}
            resume_context = artifacts.get("resume_context") or {}
            resume_context = resume_context if isinstance(resume_context, dict) else {}
            candidates = [
                ((result.get("approval_request") or {}) if isinstance(result.get("approval_request"), dict) else {}).get("request_id"),
                ((resume_context.get("approval_request") or {}) if isinstance(resume_context.get("approval_request"), dict) else {}).get("request_id"),
            ]
            if any(str(candidate or "").strip() == request_id for candidate in candidates):
                return task
        return None

    @staticmethod
    def _task_timeline_sort_key(entry: dict[str, Any]) -> tuple[float, str]:
        return (float(entry.get("timestamp") or 0.0), str(entry.get("timeline_id") or ""))

    def _build_task_timeline_payload(
        self,
        task: dict[str, Any],
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        resolved_page = max(1, int(page or 1))
        resolved_page_size = max(1, min(int(page_size or 50), 200))
        history_limit = min(max(resolved_page * resolved_page_size * 4, 100), 500)
        task_history = self.task_store.query_timeline(
            task["task_id"],
            page=1,
            page_size=history_limit,
        )
        approvals = self._related_approvals_for_task(
            approval_ids=list(task.get("approval_dependencies") or []),
            session_id=task.get("owner_session_id"),
        )
        audit_entries = self.audit_logger.query_related(
            session_id=task.get("owner_session_id"),
            task_id=task.get("task_id"),
            run_id=task.get("run_id"),
            request_ids=list(task.get("approval_dependencies") or []),
            limit=history_limit,
        )

        timeline_entries: list[dict[str, Any]] = []
        for event in task_history.get("events") or []:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") or {}
            payload = payload if isinstance(payload, dict) else {}
            step_payload = payload.get("step")
            step_payload = step_payload if isinstance(step_payload, dict) else {}
            summary = (
                str(payload.get("summary") or "").strip()
                or str(step_payload.get("output_summary") or "").strip()
                or str(event.get("event_type") or "updated")
            )
            timeline_entries.append(
                {
                    "timeline_id": str(event.get("entry_id") or ""),
                    "kind": "task_event",
                    "timestamp": float(event.get("timestamp") or 0.0),
                    "title": str(
                        step_payload.get("title")
                        or event.get("title")
                        or task.get("title")
                        or "task"
                    ),
                    "status": str(
                        step_payload.get("status")
                        or event.get("status")
                        or task.get("status")
                        or ""
                    ),
                    "event_type": str(event.get("event_type") or "updated"),
                    "summary": summary,
                    "task_id": task.get("task_id"),
                    "payload": payload,
                    "task_event": event,
                }
            )

        for approval in approvals:
            history = approval.get("history")
            history = history if isinstance(history, list) else []
            for index, history_entry in enumerate(history):
                if not isinstance(history_entry, dict):
                    continue
                state = str(history_entry.get("state") or approval.get("state") or "pending")
                reason = str(history_entry.get("reason") or "").strip()
                summary = f"{state}: {approval.get('tool_name') or approval.get('tool_pattern') or approval.get('request_id') or 'approval'}"
                if reason:
                    summary = f"{summary} — {reason}"
                timeline_entries.append(
                    {
                        "timeline_id": f"approval-{approval.get('request_id')}-{index}",
                        "kind": "approval",
                        "timestamp": float(history_entry.get("ts") or approval.get("created_at") or 0.0),
                        "title": str(approval.get("tool_name") or approval.get("tool_pattern") or "approval"),
                        "status": state,
                        "event_type": state,
                        "summary": summary,
                        "request_id": approval.get("request_id"),
                        "source_request_id": approval.get("source_request_id"),
                        "approval": approval,
                        "history_entry": history_entry,
                    }
                )

        for entry in audit_entries:
            if not isinstance(entry, dict):
                continue
            metadata = entry.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            title = str(entry.get("event_type") or entry.get("action") or "audit")
            summary_parts = [
                str(entry.get("action") or "").strip(),
                str(entry.get("resource") or "").strip(),
                str(metadata.get("resolve_reason") or "").strip(),
            ]
            summary = " · ".join(part for part in summary_parts if part) or title
            timeline_entries.append(
                {
                    "timeline_id": str(entry.get("entry_id") or ""),
                    "kind": "audit",
                    "timestamp": float(entry.get("timestamp") or 0.0),
                    "title": title,
                    "status": str(entry.get("result") or entry.get("event_type") or ""),
                    "event_type": str(entry.get("event_type") or ""),
                    "summary": summary,
                    "audit_entry_id": entry.get("entry_id"),
                    "audit_focus": {
                        "entryId": entry.get("entry_id"),
                        "requestId": metadata.get("request_id") or metadata.get("source_request_id") or entry.get("resource"),
                        "taskId": metadata.get("task_id") or task.get("task_id"),
                        "runId": metadata.get("run_id") or task.get("run_id"),
                        "sessionId": entry.get("session_id") or metadata.get("target_session_id") or task.get("owner_session_id"),
                        "toolName": metadata.get("tool_name") or metadata.get("tool_pattern"),
                        "source": metadata.get("source"),
                        "result": entry.get("result"),
                    },
                    "entry": entry,
                }
            )

        timeline_entries.sort(key=self._task_timeline_sort_key, reverse=True)
        offset = (resolved_page - 1) * resolved_page_size
        page_entries = timeline_entries[offset:offset + resolved_page_size]
        return {
            "task": task,
            "entries": page_entries,
            "pagination": {
                "page": resolved_page,
                "page_size": resolved_page_size,
                "total": len(timeline_entries),
                "has_more": offset + len(page_entries) < len(timeline_entries),
            },
        }

    def _shared_api_key_principal(self) -> str:
        api_key = self.settings.gateway_api_key or ""
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
        return f"gateway_api:{digest}"

    def _resolve_effective_user_id(
        self,
        requested_user_id: Optional[str],
        *,
        headers: Mapping[str, str],
        default_user_id: str,
    ) -> str:
        requested = (requested_user_id or "").strip()
        if not self.settings.gateway_api_key:
            return requested or default_user_id

        trusted_header = (
            getattr(self.settings, "gateway_auth_user_header", None) or ""
        ).strip()
        if trusted_header:
            authenticated_user_id = (headers.get(trusted_header) or "").strip()
            if not authenticated_user_id:
                raise HTTPException(
                    status_code=401,
                    detail=f"Missing authenticated user header: {trusted_header}",
                )
            return authenticated_user_id

        # Shared API key mode has a single authenticated principal, so caller-supplied
        # user_id values must not affect transcript ownership checks.
        return self._shared_api_key_principal()

    def _resolve_http_user_id(
        self,
        request: Request,
        requested_user_id: Optional[str],
        *,
        default_user_id: str,
    ) -> str:
        return self._resolve_effective_user_id(
            requested_user_id,
            headers=request.headers,
            default_user_id=default_user_id,
        )

    def _resolve_websocket_user_id(
        self,
        websocket: WebSocket,
        requested_user_id: Optional[str],
        *,
        default_user_id: str,
    ) -> str:
        return self._resolve_effective_user_id(
            requested_user_id,
            headers=websocket.headers,
            default_user_id=default_user_id,
        )

    async def _run_gateway_live_runtime_process_probe(
        self,
        *,
        process_kind: str,
        process_ref_prefix: str,
        gateway_mission_session_ref: str,
        supervisor_session_ref: str,
        gateway_supervisor_lifecycle_ref: str,
        source_runtime_artifact_ref: str,
        source_runtime_artifact_path: str,
        source_runtime_artifact_sha256: str,
    ) -> Dict[str, Any]:
        """Start and observe a lightweight Gateway-owned process probe.

        This intentionally does not run PX4/Gazebo or dispatch actions. It only
        proves the Gateway route started concrete observation/recovery probe tasks
        before a C5b materializer may treat the boundary as live-process evidence.
        """

        probe_id = uuid.uuid4().hex[:12]
        process_ref = f"{process_ref_prefix}{probe_id}"
        process_evidence_ref = f"gateway_live_process_probe_evidence:{probe_id}"
        task_name = f"gateway-live-runtime-probe:{process_kind}:{probe_id}"
        started_at = datetime.now(timezone.utc).isoformat()

        async def _probe_task() -> Dict[str, Any]:
            await asyncio.sleep(0)
            source_path = Path(source_runtime_artifact_path)
            source_bytes = source_path.read_bytes()
            source_runtime_sha256_verified = (
                hashlib.sha256(source_bytes).hexdigest() == source_runtime_artifact_sha256
            )
            try:
                source_runtime = json.loads(source_bytes.decode("utf-8"))
            except json.JSONDecodeError:
                source_runtime = {}
            if not isinstance(source_runtime, dict):
                source_runtime = {}
            source_runtime_supervisor_chain_observed = (
                source_runtime.get("run_mode") == "executed_run"
                and source_runtime.get("decision_loop_driver") == "mission_os_supervisor"
                and source_runtime.get("cycle_count") == 2
                and source_runtime.get("full_gateway_runtime_loop") is False
            )
            return {
                "gateway_process_probe_task_observed": True,
                "gateway_process_probe_task_name": task_name,
                "source_runtime_artifact_read_observed": True,
                "source_runtime_artifact_sha256_verified": (
                    source_runtime_sha256_verified
                ),
                "source_runtime_supervisor_chain_observed": (
                    source_runtime_supervisor_chain_observed
                ),
                "source_runtime_run_mode": source_runtime.get("run_mode"),
                "source_runtime_decision_loop_driver": source_runtime.get(
                    "decision_loop_driver"
                ),
                "source_runtime_cycle_count": source_runtime.get("cycle_count"),
                "process_completed": (
                    source_runtime_sha256_verified
                    and source_runtime_supervisor_chain_observed
                ),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }

        probe_task = asyncio.create_task(_probe_task(), name=task_name)
        task_result = await probe_task
        evidence = {
            "schema_version": "gateway_live_process_probe_evidence.v1",
            "process_kind": process_kind,
            "process_ref": process_ref,
            "process_evidence_ref": process_evidence_ref,
            "process_started": True,
            "process_started_at": started_at,
            "source_bound": True,
            "gateway_mission_session_ref": gateway_mission_session_ref,
            "supervisor_session_ref": supervisor_session_ref,
            "gateway_supervisor_lifecycle_ref": gateway_supervisor_lifecycle_ref,
            "source_runtime_artifact_ref": source_runtime_artifact_ref,
            "source_runtime_artifact_path": source_runtime_artifact_path,
            "source_runtime_artifact_sha256": source_runtime_artifact_sha256,
            "causal_verification_transferred": False,
            "physical_form1_required": True,
            "physical_form1_claimed": False,
            "physical_execution_invoked": False,
            "hardware_target_allowed": False,
            "dispatch_authority_created": False,
            "delivery_completion_claimed": False,
            "full_gateway_runtime_loop": False,
            "gateway_autonomous_runtime_claimed": False,
            **task_result,
        }
        return _sign_gateway_live_process_probe_evidence(evidence)

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------

    def _setup_routes(self):
        # --- health / root / protocol ---

        @self.app.get("/")
        async def root():
            return {
                "name": "boiled-claw Gateway",
                "version": "0.3.0",
                "protocol_version": PROTOCOL_VERSION,
                "status": "running",
                "session_backend": self.session_backend["backend"],
                "session_namespace": self.session_backend["namespace"],
                "active_sessions": len(self.manager.active_connections),
                "skills_loaded": get_skills_report().get("loaded", False),
                "skills_count": get_skills_report().get("count", 0),
            }

        @self.app.get("/health")
        async def health():
            return {
                "status": "healthy",
                "session_backend": self.session_backend["backend"],
                "session_namespace": self.session_backend["namespace"],
            }

        @self.app.get("/protocol")
        async def protocol_info():
            return {
                "version": PROTOCOL_VERSION,
                "events": list(EVENT_SCHEMAS.keys()),
                "schemas": EVENT_SCHEMAS,
                "http_surfaces": HTTP_ROUTE_SCHEMAS,
                "runtime_substrate": RUNTIME_SUBSTRATE_SCHEMA,
            }

        @self.app.get("/missionos/current-milestone")
        async def missionos_current_milestone():
            return build_current_missionos_milestone_summary()

        @self.app.get("/missionos/causal-timeline")
        async def missionos_causal_timeline():
            return build_missionos_causal_timeline_summary()

        @self.app.get("/missionos/envelopes")
        async def missionos_envelopes():
            return build_missionos_envelope_browser_summary()

        @self.app.get("/missionos/knowledge")
        async def missionos_knowledge():
            return build_missionos_knowledge_browser_summary()

        @self.app.get("/missionos/agents")
        async def missionos_agents():
            return build_missionos_agent_dashboard_summary()

        @self.app.get("/missionos/capabilities")
        async def missionos_capabilities():
            return build_missionos_capability_registry_summary()

        @self.app.get("/missionos/knowledge-sharing")
        async def missionos_knowledge_sharing():
            return build_missionos_knowledge_sharing_summary()

        @self.app.post("/missionos/knowledge-sharing/curate-dry-run")
        async def missionos_knowledge_sharing_curate_dry_run():
            return await run_in_threadpool(run_knowledge_curator_dry_run)

        @self.app.post("/missionos/knowledge-sharing/publish")
        async def missionos_knowledge_sharing_publish():
            return await run_in_threadpool(run_knowledge_curator_production_publish)

        @self.app.get("/missionos/policy-authority")
        async def missionos_policy_authority():
            return build_policy_authority_summary()

        @self.app.post("/missionos/policy-authority/promote")
        async def missionos_policy_authority_promote():
            return await run_in_threadpool(run_policy_authority_promotion)

        @self.app.get("/missionos/form2a-response-selection")
        async def missionos_form2a_response_selection():
            return _missionos_internal_capability_route_response(
                build_form2a_response_selection_summary(),
                capability_id="form2a_response_selection",
                source_route="/missionos/form2a-response-selection",
            )

        @self.app.post("/missionos/form2a-response-selection/run")
        async def missionos_form2a_response_selection_run(request: Request):
            payload: dict[str, Any] = {}
            try:
                raw_payload = await request.json()
                payload = raw_payload if isinstance(raw_payload, dict) else {}
            except Exception:
                payload = {}
            return await run_in_threadpool(
                lambda: _missionos_internal_capability_route_response(
                    run_form2a_response_selection_from_form1(
                        operator_instruction=payload.get("operator_instruction"),
                    ),
                    capability_id="form2a_response_selection",
                    source_route="/missionos/form2a-response-selection/run",
                )
            )

        @self.app.post("/missionos/autonomy-conversation/run")
        async def missionos_autonomy_conversation_run(request: Request):
            payload: dict[str, Any] = {}
            try:
                raw_payload = await request.json()
                payload = raw_payload if isinstance(raw_payload, dict) else {}
            except Exception:
                payload = {}
            return await run_in_threadpool(run_missionos_autonomy_conversation, payload)

        @self.app.get("/missionos/form2a-operator-review")
        async def missionos_form2a_operator_review():
            return build_form2a_operator_review_summary()

        @self.app.post("/missionos/form2a-operator-review/approve")
        async def missionos_form2a_operator_review_approve():
            return await run_in_threadpool(run_form2a_operator_review_approve)

        @self.app.post("/missionos/form2a-operator-review/reject")
        async def missionos_form2a_operator_review_reject():
            return await run_in_threadpool(run_form2a_operator_review_reject)

        @self.app.post("/missionos/form2a-operator-review/request-revision")
        async def missionos_form2a_operator_review_request_revision():
            return await run_in_threadpool(run_form2a_operator_review_request_revision)

        @self.app.get("/missionos/form2a-action-consumption")
        async def missionos_form2a_action_consumption():
            return build_form2a_action_consumption_summary()

        @self.app.post("/missionos/form2a-action-consumption/run")
        async def missionos_form2a_action_consumption_run():
            return await run_in_threadpool(run_form2a_action_consumption)

        @self.app.get("/missionos/llm-repair-planner")
        async def missionos_llm_repair_planner():
            return _missionos_internal_capability_route_response(
                build_llm_repair_planner_summary(),
                capability_id="llm_repair_planning",
                source_route="/missionos/llm-repair-planner",
            )

        @self.app.post("/missionos/llm-repair-planner/run")
        async def missionos_llm_repair_planner_run():
            return await run_in_threadpool(
                lambda: _missionos_internal_capability_route_response(
                    run_llm_repair_planner_from_latest_evidence(),
                    capability_id="llm_repair_planning",
                    source_route="/missionos/llm-repair-planner/run",
                )
            )

        @self.app.post("/missionos/runtime-recovery-agent/run")
        async def missionos_runtime_recovery_agent_run(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            telemetry_snapshot = body.get("telemetry_snapshot")
            if not isinstance(telemetry_snapshot, dict):
                raise HTTPException(
                    status_code=400,
                    detail="telemetry_snapshot object is required",
                )
            mission_context = body.get("mission_context")
            recovery_policy = body.get("recovery_policy")
            return await run_in_threadpool(
                run_missionos_runtime_recovery_agent,
                telemetry_snapshot=telemetry_snapshot,
                mission_context=(
                    mission_context if isinstance(mission_context, dict) else {}
                ),
                recovery_policy=(
                    recovery_policy if isinstance(recovery_policy, dict) else {}
                ),
            )

        @self.app.post("/missionos/runtime-recovery-agent/propose-for-task")
        async def missionos_runtime_recovery_agent_propose_for_task(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            task_id = str(body.get("task_id") or "").strip()
            if not task_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            task = self.task_store.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            artifacts = task.get("artifacts")
            artifacts = artifacts if isinstance(artifacts, Mapping) else {}
            telemetry_snapshot = _runtime_recovery_telemetry_from_task_artifacts(artifacts)
            if not telemetry_snapshot:
                recovery_policy = _operator_recovery_proposal_policy()
                planner_result = {
                    "schema_version": (
                        "missionos_runtime_recovery_planner_tool_result.v1"
                    ),
                    "tool_name": "missionos_plan_bounded_recovery_maneuver",
                    "tool_status": "insufficient_context",
                    "requested_action": str(body.get("requested_action") or "").strip(),
                    "request_reason": str(body.get("operator_instruction") or "")[:500],
                    "recommended_candidate": {},
                    "candidates": [],
                    "candidate_actions": [],
                    "dispatch_authority_created": False,
                    "operator_approval_required": True,
                    "physical_execution_invoked": False,
                    "progress_counted": False,
                }
                return JSONResponse(
                    status_code=409,
                    content=_runtime_recovery_operator_proposal_response(
                        task_id=task_id,
                        operator_instruction=str(
                            body.get("operator_instruction") or ""
                        ).strip(),
                        requested_action=planner_result["requested_action"],
                        requested_parameters={},
                        telemetry_snapshot={},
                        recovery_policy=recovery_policy,
                        planner_result=planner_result,
                    ),
                )
            operator_instruction = str(body.get("operator_instruction") or "").strip()
            requested_action = str(body.get("requested_action") or "").strip()
            raw_parameters = body.get("requested_parameters")
            requested_parameters = (
                dict(raw_parameters) if isinstance(raw_parameters, Mapping) else {}
            )
            recovery_policy = _operator_recovery_proposal_policy()
            mission_context = {
                "task_id": task_id,
                "operator_instruction": operator_instruction,
                "operator_recovery_request": {
                    "requested_action": requested_action,
                    "source": "operator_natural_language_chat",
                    **requested_parameters,
                },
                "authority_status": "proposal_only",
            }
            planner_result = await run_in_threadpool(
                plan_runtime_recovery_maneuver,
                telemetry_snapshot=telemetry_snapshot,
                mission_context=mission_context,
                recovery_policy=recovery_policy,
                requested_action=requested_action,
                request_reason=operator_instruction,
            )
            planner_result = guard_runtime_recovery_planner_result(
                planner_result=planner_result,
                telemetry_snapshot=telemetry_snapshot,
                recovery_policy=recovery_policy,
            )
            return _runtime_recovery_operator_proposal_response(
                task_id=task_id,
                operator_instruction=operator_instruction,
                requested_action=requested_action,
                requested_parameters=requested_parameters,
                telemetry_snapshot=telemetry_snapshot,
                recovery_policy=recovery_policy,
                planner_result=planner_result,
            )

        @self.app.post("/missionos/llm-repair-planner/run-for-task")
        async def missionos_llm_repair_planner_run_for_task(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            task_id = str(body.get("task_id") or "").strip()
            if not task_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            task = self.task_store.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            artifacts = task.get("artifacts") or {}
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            metadata = task.get("metadata") or {}
            metadata = metadata if isinstance(metadata, dict) else {}
            summary = body.get("summary") if isinstance(body.get("summary"), dict) else {}
            blocked_reasons = list(summary.get("blocked_reasons") or [])
            failure_category = (
                summary.get("failure_category")
                or metadata.get("mission_designer_live_sitl_failure_category")
                or metadata.get("failure_category")
                or ""
            )
            if failure_category and failure_category not in blocked_reasons:
                blocked_reasons.append(str(failure_category))
            evidence = {
                "schema_version": "missionos_task_bound_repair_input.v1",
                "task_id": task_id,
                "task_status": task.get("status"),
                "kind": task.get("kind"),
                "title": task.get("title"),
                "summary_status": summary.get("task_status")
                or summary.get("result_status")
                or task.get("status"),
                "blocking_reasons": blocked_reasons,
                "summary": summary,
                "metadata": metadata,
                "artifacts": artifacts,
                "source_boundary": {
                    "source": "server_task_store",
                    "client_supplied_evidence_trusted": False,
                    "task_refetched_server_side": True,
                    "operator_approved": False,
                    "dispatch_authority_created": False,
                    "progress_counted": False,
                },
            }
            return await run_in_threadpool(
                lambda: _missionos_internal_capability_route_response(
                    run_llm_repair_planner_from_evidence_payload(
                        evidence_artifact=evidence,
                        evidence_label="mission_designer_live_sitl_task",
                    ),
                    capability_id="llm_repair_planning",
                    source_route="/missionos/llm-repair-planner/run-for-task",
                )
            )

        @self.app.get("/missionos/sitl-dispatch-execution")
        async def missionos_sitl_dispatch_execution():
            return build_sitl_dispatch_execution_summary()

        @self.app.post("/missionos/sitl-dispatch-execution/run")
        async def missionos_sitl_dispatch_execution_run():
            return await run_in_threadpool(run_sitl_bounded_dispatch_execution)

        @self.app.post("/missionos/real-hardware-arm-disarm-dispatch/run")
        async def missionos_real_hardware_arm_disarm_dispatch_run(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            task_id = str(body.get("task_id") or "").strip()
            if not task_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            if self.task_store.get(task_id) is None:
                raise HTTPException(status_code=404, detail="task not found")
            subject_id = str(body.get("subject_id") or "").strip()
            if not subject_id:
                raise HTTPException(status_code=400, detail="subject_id is required")
            attestation = body.get("physical_attestation")
            if not isinstance(attestation, dict):
                raise HTTPException(
                    status_code=400,
                    detail="physical_attestation object is required",
                )
            # The operator's physical attestation and approval are Gateway-collected
            # inputs; the orchestration never self-approves. Real serial actuation
            # additionally requires the executor opt-in env gate, which is off here
            # by default, so without it the chain stops honestly at the executor.
            try:
                actuator_approval = build_px4_real_hardware_actuator_approval(
                    approved_operations=("arm", "disarm"),
                    physical_attestation=attestation,
                )
            except (PX4RealHardwareActuatorError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            def _run() -> Dict[str, Any]:
                return run_real_hardware_arm_disarm_dispatch(
                    store=self.task_store,
                    task_id=task_id,
                    subject_id=subject_id,
                    artifact_root=ARTIFACT_ROOT,
                    artifact_relative=_relative,
                    authority_table_state_path=(
                        Path(ARTIFACT_ROOT)
                        / "missionos_real_hardware_dispatch_authority"
                        / "authority_table_state.json"
                    ),
                    actuator_approval=actuator_approval,
                    operator_approved=body.get("operator_approved") is True,
                    bench_context=(
                        body.get("bench_context")
                        if isinstance(body.get("bench_context"), dict)
                        else None
                    ),
                    operator_instruction=(
                        body.get("operator_instruction")
                        if isinstance(body.get("operator_instruction"), dict)
                        else None
                    ),
                    serial_device=(
                        str(body["serial_device"])
                        if body.get("serial_device")
                        else None
                    ),
                    opt_in=body.get("opt_in") is True,
                )

            return await run_in_threadpool(_run)

        @self.app.get("/missionos/scoped-form3-closed-loop")
        async def missionos_scoped_form3_closed_loop():
            return build_scoped_form3_closed_loop_summary()

        @self.app.post("/missionos/scoped-form3-closed-loop/run")
        async def missionos_scoped_form3_closed_loop_run():
            return await run_in_threadpool(run_scoped_form3_closed_loop)

        @self.app.get("/missionos/operations")
        async def missionos_operations():
            return get_missionos_operations_registry()

        @self.app.get("/missionos/operations/{operation_id}/last")
        async def missionos_operation_last(operation_id: str):
            try:
                return get_missionos_operation_last(operation_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="unknown operation_id") from exc

        @self.app.post("/missionos/operations/{operation_id}/run")
        async def missionos_operation_run(
            operation_id: str,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            try:
                return await run_in_threadpool(
                    run_missionos_operation,
                    operation_id,
                    payload=payload or {},
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="unknown operation_id") from exc

        @self.app.get("/missionos/operations/runs/{run_id}")
        async def missionos_operation_run_status(run_id: str):
            try:
                return get_missionos_operation_run(run_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="unknown run_id") from exc

        @self.app.get("/missionos/operations/runs/{run_id}/artifact")
        async def missionos_operation_run_artifact(run_id: str):
            try:
                return get_missionos_operation_run_artifact(run_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="unknown run_id") from exc

        @self.app.post(GATEWAY_ROUTE_INVOCATION_BOUNDARY_PATH)
        async def gateway_live_runtime_process_boundary(
            request: Request,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            client_host = request.client.host if request.client else ""
            if client_host not in {"127.0.0.1", "::1", "localhost"}:
                raise HTTPException(
                    status_code=403,
                    detail="Gateway process-boundary probe is loopback-only",
                )
            try:
                return build_gateway_route_invocation_boundary(
                    payload or {},
                    client_host=client_host,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @self.app.post(GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_PATH)
        async def gateway_live_runtime_supervisor_process_probe(
            request: Request,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            client_host = request.client.host if request.client else ""
            if client_host not in {"127.0.0.1", "::1", "localhost"}:
                raise HTTPException(
                    status_code=403,
                    detail="Gateway supervisor process probe is loopback-only",
                )
            try:
                route_payload = payload or {}
                def _payload_string(key: str) -> str:
                    value = route_payload.get(key)
                    if not isinstance(value, str) or not value:
                        raise ValueError(f"{key} is required")
                    return value

                process_probe_evidence = {
                    "observation": await self._run_gateway_live_runtime_process_probe(
                        process_kind=GATEWAY_OBSERVATION_PROCESS_PROBE_KIND,
                        process_ref_prefix="gateway_live_observation_process_probe:",
                        gateway_mission_session_ref=_payload_string(
                            "gateway_mission_session_ref"
                        ),
                        supervisor_session_ref=_payload_string(
                            "supervisor_session_ref"
                        ),
                        gateway_supervisor_lifecycle_ref=_payload_string(
                            "gateway_supervisor_lifecycle_ref"
                        ),
                        source_runtime_artifact_ref=_payload_string(
                            "source_runtime_artifact_ref"
                        ),
                        source_runtime_artifact_path=_payload_string(
                            "source_runtime_artifact_path"
                        ),
                        source_runtime_artifact_sha256=_payload_string(
                            "source_runtime_artifact_sha256"
                        ),
                    ),
                    "recovery_decision": await self._run_gateway_live_runtime_process_probe(
                        process_kind=GATEWAY_RECOVERY_DECISION_PROCESS_PROBE_KIND,
                        process_ref_prefix=(
                            "gateway_live_recovery_decision_process_probe:"
                        ),
                        gateway_mission_session_ref=_payload_string(
                            "gateway_mission_session_ref"
                        ),
                        supervisor_session_ref=_payload_string(
                            "supervisor_session_ref"
                        ),
                        gateway_supervisor_lifecycle_ref=_payload_string(
                            "gateway_supervisor_lifecycle_ref"
                        ),
                        source_runtime_artifact_ref=_payload_string(
                            "source_runtime_artifact_ref"
                        ),
                        source_runtime_artifact_path=_payload_string(
                            "source_runtime_artifact_path"
                        ),
                        source_runtime_artifact_sha256=_payload_string(
                            "source_runtime_artifact_sha256"
                        ),
                    ),
                }
                return build_gateway_supervisor_process_probe_boundary_from_route(
                    route_payload,
                    client_host=client_host,
                    process_probe_evidence=process_probe_evidence,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        # --- skills ---

        @self.app.get("/skills")
        async def skills():
            await ensure_skills_loaded()
            detail = await tool_skill_list()
            report = get_skills_report()
            return {**report, "details": detail.get("skills", [])}

        @self.app.post("/skills/{skill_name}/execute")
        async def execute_skill(skill_name: str, payload: Dict[str, Any] | None = Body(default=None)):
            params = {}
            if payload and isinstance(payload.get("params"), dict):
                params = payload.get("params", {})
            result = await tool_skill_execute(skill_name, json.dumps(params, ensure_ascii=False))
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("message", "Skill execution failed"))
            return result

        # --- runtime substrate ---

        @self.app.get("/runtime/resources")
        async def runtime_resources():
            return await tool_resource_list()

        @self.app.get("/runtime/resources/{resource_id:path}")
        async def runtime_resource(resource_id: str, refresh: bool = Query(default=False)):
            result = await tool_resource_read(resource_id, refresh=refresh)
            if not result.get("ok"):
                raise HTTPException(status_code=404, detail=result.get("message", "Resource not found"))
            return result

        @self.app.get("/runtime/capabilities")
        async def runtime_capabilities(refresh: bool = Query(default=False)):
            return await tool_capability_list(refresh=refresh)

        @self.app.post("/runtime/capabilities/invoke")
        async def runtime_capability_invoke(payload: Dict[str, Any] | None = Body(default=None)):
            if not payload or not isinstance(payload.get("name"), str) or not payload.get("name"):
                raise HTTPException(status_code=400, detail="name is required")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise HTTPException(status_code=400, detail="params must be an object")
            result = await tool_capability_invoke(
                payload["name"],
                json.dumps(params, ensure_ascii=False),
            )
            if not result.get("success") and str(result.get("error", "")).startswith("Unknown capability:"):
                raise HTTPException(status_code=400, detail=result["error"])
            if not result.get("success") and "requires tool_context-backed approval flow" in str(
                result.get("error", "")
            ):
                raise HTTPException(status_code=403, detail=result["error"])
            return result

        # --- PX4/Gazebo Mission Designer ---

        @self.app.post("/px4-gazebo/mission-scenarios/propose")
        async def px4_gazebo_mission_scenario_propose(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            coordinate_route = (
                body.get("coordinate_route")
                if isinstance(body.get("coordinate_route"), dict)
                else None
            )
            prompt = str(body.get("prompt") or "").strip()
            if not prompt and coordinate_route:
                prompt = "Coordinate Route planning request"
            if not prompt:
                raise HTTPException(status_code=400, detail="prompt is required")
            try:
                result = run_px4_gazebo_mission_scenario_designer(
                    prompt=prompt,
                    coordinate_route=coordinate_route,
                    now=datetime.now(timezone.utc),
                )
                return enrich_terrain_heightmap_preview_fields(result)
            except PX4GazeboMissionScenarioDesignerError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @self.app.post("/px4-gazebo/mission-scenarios/approve")
        async def px4_gazebo_mission_scenario_approve(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            proposal = body.get("scenario_proposal")
            validation = body.get("validation_result")
            if not isinstance(proposal, dict):
                raise HTTPException(
                    status_code=400,
                    detail="scenario_proposal is required",
                )
            if not isinstance(validation, dict):
                raise HTTPException(
                    status_code=400,
                    detail="validation_result is required",
                )
            try:
                return approve_px4_gazebo_mission_scenario_for_bounded_simulation(
                    proposal=proposal,
                    validation=validation,
                    now=datetime.now(timezone.utc),
                )
            except (PX4GazeboMissionScenarioDesignerError, ValidationError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @self.app.post("/px4-gazebo/mission-scenarios/prepare-sitl-execution")
        async def px4_gazebo_mission_scenario_prepare_sitl_execution(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            proposal = body.get("scenario_proposal")
            validation = body.get("validation_result")
            approval = body.get("scenario_approval")
            compile_result = body.get("scenario_compile_result")
            bounded_request = body.get("bounded_simulation_request")
            required_objects = {
                "scenario_proposal": proposal,
                "validation_result": validation,
                "scenario_approval": approval,
                "scenario_compile_result": compile_result,
                "bounded_simulation_request": bounded_request,
            }
            for field_name, value in required_objects.items():
                if not isinstance(value, dict):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{field_name} is required",
                    )
            try:
                execution_request = (
                    build_px4_gazebo_mission_designer_sitl_execution_request(
                        proposal=proposal,
                        validation=validation,
                        approval=approval,
                        compile_result=compile_result,
                        bounded_simulation_request=bounded_request,
                        now=datetime.now(timezone.utc),
                    )
                )
            except (PX4GazeboMissionScenarioDesignerError, ValidationError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            execution_request_payload = execution_request.model_dump(mode="json")
            artifacts = {
                "px4_gazebo_mission_scenario_proposal": proposal,
                "px4_gazebo_mission_scenario_validation_result": validation,
                "px4_gazebo_mission_scenario_approval": approval,
                "px4_gazebo_mission_scenario_compile_result": compile_result,
                "px4_gazebo_bounded_simulation_request": bounded_request,
                "px4_gazebo_mission_designer_sitl_execution_request": execution_request_payload,
            }
            optional_artifacts = {
                "mission_designer_coordinate_pair_route": body.get(
                    "mission_designer_coordinate_pair_route"
                ),
                "real_world_target_resolution": body.get(
                    "real_world_target_resolution"
                ),
                "terrain_dem_source_snapshot": body.get(
                    "terrain_dem_source_snapshot"
                ),
                "terrain_heightmap_file_artifact": body.get(
                    "terrain_heightmap_file_artifact"
                ),
                "execution_terrain_fallback_reason": body.get(
                    "execution_terrain_fallback_reason"
                ),
                "execution_terrain_source_backed": body.get(
                    "execution_terrain_source_backed"
                ),
                "gazebo_world_artifact": body.get("gazebo_world_artifact"),
                "coordinate_transform_candidate": body.get(
                    "coordinate_transform_candidate"
                ),
                "digital_twin_sitl_binding_gate": body.get(
                    "digital_twin_sitl_binding_gate"
                ),
                "digital_twin_route_plan": body.get("digital_twin_route_plan"),
                "digital_twin_px4_mission_item_candidate": body.get(
                    "digital_twin_px4_mission_item_candidate"
                ),
                "mission_scenario_designer_summary": body.get("summary"),
            }
            artifacts.update(
                {
                    key: value
                    for key, value in optional_artifacts.items()
                    if isinstance(value, dict)
                }
            )
            artifacts = enrich_terrain_heightmap_preview_fields(artifacts)
            coordinate_route_artifact = artifacts.get(
                "mission_designer_coordinate_pair_route"
            )
            if isinstance(coordinate_route_artifact, Mapping):
                artifacts["mission_designer_coordinate_pair_sitl_binding"] = (
                    _mission_designer_coordinate_pair_sitl_binding(
                        route=coordinate_route_artifact,
                        scenario_approval=approval,
                        bounded_request=bounded_request,
                    )
                )
            operator_route_requested = _mission_designer_operator_route_requested(
                artifacts=artifacts,
                metadata={},
            )
            route_bound_to_sitl = _mission_designer_route_bound_to_sitl(artifacts)
            task = self.task_store.create(
                kind="px4_gazebo_mission_designer_sitl_execution_request",
                title="PX4/Gazebo Mission Designer SITL execution request",
                status="pending",
                owner_session_id=str(body.get("owner_session_id") or "").strip()
                or None,
                owner_user_id=str(body.get("owner_user_id") or "").strip() or None,
                parent_task_id=str(body.get("parent_task_id") or "").strip() or None,
                artifacts=artifacts,
                metadata={
                    "source": "px4_gazebo_mission_scenario_prepare_sitl_execution",
                    "execution_request_id": execution_request.execution_request_id,
                    "request_status": execution_request.request_status,
                    "execution_mode": execution_request.execution_mode,
                    "preparation_scope": execution_request.preparation_scope,
                    "requires_explicit_execution_approval": execution_request.requires_explicit_execution_approval,
                    "execution_invoked": execution_request.execution_invoked,
                    "gazebo_execution_invoked": execution_request.gazebo_execution_invoked,
                    "external_dispatch_performed": execution_request.external_dispatch_performed,
                    "mavlink_dispatch_performed": execution_request.mavlink_dispatch_performed,
                    "px4_mission_upload_performed": execution_request.px4_mission_upload_performed,
                    "hardware_target_allowed": execution_request.hardware_target_allowed,
                    "physical_execution_invoked": execution_request.physical_execution_invoked,
                    "operator_route_requested": operator_route_requested,
                    "operator_route_bound_to_sitl": route_bound_to_sitl,
                    "operator_route_blocked_reason": (
                        _MISSION_DESIGNER_COORDINATE_ROUTE_BLOCKED_REASON
                        if operator_route_requested and not route_bound_to_sitl
                        else ""
                    ),
                },
            )
            return {
                "sitl_execution_request": execution_request_payload,
                "mission_designer_coordinate_pair_sitl_binding": artifacts.get(
                    "mission_designer_coordinate_pair_sitl_binding"
                ),
                "task": task,
                "summary": {
                    "task_id": task["task_id"],
                    "task_status": task["status"],
                    "request_status": execution_request.request_status,
                    "execution_mode": execution_request.execution_mode,
                    "preparation_scope": execution_request.preparation_scope,
                    "target_endpoint": execution_request.target_endpoint,
                    "target_endpoint_whitelisted": execution_request.target_endpoint_whitelisted,
                    "requires_explicit_execution_approval": execution_request.requires_explicit_execution_approval,
                    "execution_invoked": execution_request.execution_invoked,
                    "gazebo_execution_invoked": execution_request.gazebo_execution_invoked,
                    "external_dispatch_performed": execution_request.external_dispatch_performed,
                    "mavlink_dispatch_performed": execution_request.mavlink_dispatch_performed,
                    "px4_mission_upload_performed": execution_request.px4_mission_upload_performed,
                    "hardware_target_allowed": execution_request.hardware_target_allowed,
                    "physical_execution_invoked": execution_request.physical_execution_invoked,
                    "operator_route_requested": operator_route_requested,
                    "operator_route_bound_to_sitl": route_bound_to_sitl,
                    "operator_route_blocked_reason": (
                        _MISSION_DESIGNER_COORDINATE_ROUTE_BLOCKED_REASON
                        if operator_route_requested and not route_bound_to_sitl
                        else ""
                    ),
                },
            }

        def _mission_designer_sitl_readiness_response(
            *,
            task_id: str,
            task: Mapping[str, Any],
            readiness: Mapping[str, Any],
            status_code: int = 200,
        ) -> JSONResponse:
            ready = readiness.get("readiness_status") == "ready"
            blocked_reasons = [
                str(item) for item in (readiness.get("blocked_reasons") or ())
            ]
            content = {
                "schema_version": "px4_gazebo_mission_designer_sitl_execution_readiness_response.v1",
                "task": dict(task),
                "artifacts": {
                    "px4_gazebo_sitl_execution_readiness": dict(readiness),
                },
                "px4_gazebo_sitl_execution_readiness": dict(readiness),
                "summary": {
                    "task_id": task_id,
                    "task_status": "ready" if ready else "blocked",
                    "result_status": "ready" if ready else "blocked",
                    "readiness_status": readiness.get("readiness_status"),
                    "failure_category": "" if ready else "sitl_endpoint_not_ready",
                    "blocked_reasons": blocked_reasons,
                    "endpoint_host": readiness.get("endpoint_host"),
                    "mavlink_udp_port": readiness.get("mavlink_udp_port"),
                    "docker_container_running": readiness.get(
                        "docker_container_running"
                    )
                    is True,
                    "mavlink_endpoint_observed": readiness.get(
                        "mavlink_endpoint_observed"
                    )
                    is True,
                    "sitl_startup_action_available": readiness.get(
                        "sitl_startup_action_available"
                    )
                    is True,
                    "startup_action_will_start_container": readiness.get(
                        "startup_action_will_start_container"
                    )
                    is True,
                    "upload_status": "not_attempted",
                    "mission_ack_observed": False,
                    "mission_upload_allowed": readiness.get(
                        "mission_upload_allowed"
                    )
                    is True,
                    "live_flight_runner_allowed": readiness.get(
                        "live_flight_runner_allowed"
                    )
                    is True,
                    "live_flight_runner_invoked": False,
                    "execution_attempted": False,
                    "progress_counted": False,
                    "dispatch_authority_created": False,
                    "delivery_completion_claimed": False,
                    "hardware_target_allowed": False,
                    "physical_execution_invoked": False,
                    "operator_message": (
                        "PX4/Gazebo SITL is ready for Live SITL execution."
                        if ready
                        else "PX4/Gazebo SITL is not running. Start the simulator before executing Live SITL."
                    ),
                },
            }
            return JSONResponse(status_code=status_code, content=content)

        def _mission_designer_sitl_startup_action_available() -> bool:
            return os.getenv(PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_OPT_IN_ENV) == "1"

        def _start_mission_designer_sitl_container(task_id: str) -> dict[str, Any]:
            import scripts.smoke_px4_gazebo_sitl_mission_upload as upload_smoke

            container_name = upload_smoke.CONTAINER_NAME
            started_at = datetime.now(timezone.utc).isoformat()
            upload_smoke._start_container()
            os.environ[PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_OPT_IN_ENV] = "1"
            os.environ[PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_CONTAINER_ENV] = (
                container_name
            )
            os.environ[PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_REUSE_CONTAINER_ENV] = "1"
            # NOTE: RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION and
            # RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT are execution
            # authority env vars and must be set at Gateway process startup —
            # NOT here. /start-sitl is an environment startup action only.
            completed_at = datetime.now(timezone.utc).isoformat()
            readiness = build_px4_gazebo_sitl_execution_readiness(
                endpoint_host="127.0.0.1",
                mavlink_udp_port=14540,
                docker_required=True,
                timeout_seconds=2.0,
                sitl_startup_action_available=True,
            )
            return {
                "schema_version": "px4_gazebo_mission_designer_sitl_startup.v1",
                "startup_status": "started",
                "task_id": task_id,
                "container_name": container_name,
                "docker_exec_uploader_enabled": True,
                "docker_exec_uploader_reuse_container": True,
                "mission_upload_performed": False,
                "live_flight_runner_invoked": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
                "started_at": started_at,
                "completed_at": completed_at,
                "readiness": readiness,
            }

        @self.app.post("/px4-gazebo/mission-scenarios/execute-sitl-readiness")
        async def px4_gazebo_mission_scenario_execute_sitl_readiness(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            task_id = str(body.get("task_id") or "").strip()
            if not task_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            task = self.task_store.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            readiness = await run_in_threadpool(
                build_px4_gazebo_sitl_execution_readiness,
                endpoint_host="127.0.0.1",
                mavlink_udp_port=14540,
                docker_required=True,
                timeout_seconds=2.0,
                sitl_startup_action_available=(
                    _mission_designer_sitl_startup_action_available()
                ),
            )
            return _mission_designer_sitl_readiness_response(
                task_id=task_id,
                task=task,
                readiness=readiness,
                status_code=200,
            )

        @self.app.post("/px4-gazebo/mission-scenarios/start-sitl")
        async def px4_gazebo_mission_scenario_start_sitl(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            task_id = str(body.get("task_id") or "").strip()
            if not task_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            task = self.task_store.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            try:
                startup = await run_in_threadpool(
                    _start_mission_designer_sitl_container,
                    task_id,
                )
            except Exception as exc:
                failed = {
                    "schema_version": "px4_gazebo_mission_designer_sitl_startup.v1",
                    "startup_status": "blocked",
                    "task_id": task_id,
                    "blocked_reasons": ["px4_gazebo_sitl_startup_failed"],
                    "failure_message": str(exc),
                    "mission_upload_performed": False,
                    "live_flight_runner_invoked": False,
                    "hardware_target_allowed": False,
                    "physical_execution_invoked": False,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                }
                blocked_task = self.task_store.update(
                    task_id,
                    status="blocked",
                    artifacts={
                        "px4_gazebo_mission_designer_sitl_startup": failed,
                    },
                    metadata={
                        "mission_designer_sitl_startup_status": "blocked",
                        "mission_designer_sitl_startup_failed": True,
                        "hardware_target_allowed": False,
                        "physical_execution_invoked": False,
                    },
                )
                return JSONResponse(
                    status_code=409,
                    content={
                        "task": blocked_task or task,
                        "px4_gazebo_mission_designer_sitl_startup": failed,
                        "summary": {
                            "task_id": task_id,
                            "task_status": "blocked",
                            "startup_status": "blocked",
                            "failure_category": "px4_gazebo_sitl_startup_failed",
                            "blocked_reasons": failed["blocked_reasons"],
                            "mission_upload_performed": False,
                            "live_flight_runner_invoked": False,
                            "hardware_target_allowed": False,
                            "physical_execution_invoked": False,
                        },
                    },
                )
            task_artifacts = (
                task.get("artifacts") if isinstance(task.get("artifacts"), Mapping) else {}
            )
            task_metadata = (
                task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
            )
            previous_startup = task_artifacts.get(
                "px4_gazebo_mission_designer_sitl_startup"
            )
            previous_startup_reasons_raw = (
                previous_startup.get("blocked_reasons")
                if isinstance(previous_startup, Mapping)
                else []
            )
            previous_startup_reasons = (
                [str(reason) for reason in previous_startup_reasons_raw]
                if isinstance(previous_startup_reasons_raw, list)
                else []
            )
            startup_retry_recovered = (
                task.get("status") == "blocked"
                and (
                    task_metadata.get("mission_designer_sitl_startup_failed") is True
                    or (
                        isinstance(previous_startup, Mapping)
                        and previous_startup.get("startup_status") == "blocked"
                        and "px4_gazebo_sitl_startup_failed"
                        in previous_startup_reasons
                    )
                )
            )
            if startup_retry_recovered:
                startup["recovered_previous_startup_failure"] = True
                startup["previous_startup_blocked_reasons"] = list(
                    previous_startup_reasons
                )
            updated_task = self.task_store.update(
                task_id,
                status="pending" if startup_retry_recovered else None,
                artifacts={
                    "px4_gazebo_mission_designer_sitl_startup": startup,
                },
                metadata={
                    "mission_designer_sitl_startup_status": "started",
                    "mission_designer_sitl_startup_failed": False,
                    "mission_designer_sitl_startup_recovered_from_failure": (
                        startup_retry_recovered
                    ),
                    "mission_designer_sitl_startup_container_name": startup[
                        "container_name"
                    ],
                    "hardware_target_allowed": False,
                    "physical_execution_invoked": False,
                },
            )
            readiness = startup["readiness"]
            return {
                "task": updated_task or task,
                "px4_gazebo_mission_designer_sitl_startup": startup,
                "px4_gazebo_sitl_execution_readiness": readiness,
                "summary": {
                    "task_id": task_id,
                    "task_status": (updated_task or task).get("status"),
                    "startup_status": "started",
                    "container_name": startup["container_name"],
                    "readiness_status": readiness.get("readiness_status"),
                    "startup_recovered_from_failure": startup_retry_recovered,
                    "sitl_startup_action_available": True,
                    "mission_upload_performed": False,
                    "live_flight_runner_invoked": False,
                    "hardware_target_allowed": False,
                    "physical_execution_invoked": False,
                },
            }

        @self.app.post("/px4-gazebo/mission-scenarios/recovery-dispatch")
        async def px4_gazebo_mission_scenario_recovery_dispatch(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            task_id = str(body.get("task_id") or "").strip()
            recovery_action = str(body.get("recovery_action") or "").strip()
            if not task_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            if recovery_action not in MISSIONOS_RUNTIME_RECOVERY_ACTIONS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "recovery_action must be one of "
                        + ", ".join(sorted(MISSIONOS_RUNTIME_RECOVERY_ACTIONS))
                    ),
                )
            if body.get("explicit_recovery_dispatch_approval") is not True:
                raise HTTPException(
                    status_code=400,
                    detail="explicit_recovery_dispatch_approval is required",
                )
            recovery_parameters = _bounded_operator_recovery_parameters(
                recovery_action=recovery_action,
                body=body,
            )
            task = self.task_store.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            artifacts = (
                task.get("artifacts") if isinstance(task.get("artifacts"), Mapping) else {}
            )
            running_receipt = artifacts.get(
                "missionos_auto_mission_gui_dispatch_running_receipt"
            )
            blocked_reasons: list[str] = []
            if task.get("status") != "running":
                blocked_reasons.append("missionos_auto_mission_task_not_running")
            if not isinstance(running_receipt, Mapping):
                blocked_reasons.append("missionos_auto_mission_running_receipt_missing")
            operator_recovery_request_container_path = (
                str(running_receipt.get("operator_recovery_request_container_path") or "")
                if isinstance(running_receipt, Mapping)
                else ""
            )

            now = datetime.now(timezone.utc)
            approval, allowlist = _operator_recovery_approval_payload(
                recovery_action=recovery_action,
                task_id=task_id,
                parameters=recovery_parameters,
                now=now,
            )

            operator_recovery_request = {
                "schema_version": "missionos_auto_operator_recovery_request.v1",
                "task_id": task_id,
                "request_status": "queued",
                "recovery_action": recovery_action,
                "recovery_parameters": recovery_parameters,
                "operator_approved": True,
                "explicit_recovery_dispatch_approval": True,
                "approval_ref": (
                    approval.approval_id
                    if hasattr(approval, "approval_id")
                    else approval.get("approval_id")
                    if isinstance(approval, Mapping)
                    else ""
                ),
                "operator_surface": "missionos_runtime_recovery",
                "delivery_completion_claimed": False,
                "progress_counted": False,
                "physical_execution_invoked": False,
                "hardware_target_allowed": False,
                "observed_at": now.isoformat(),
            }
            active_runner_request_write: dict[str, Any] | None = None
            if blocked_reasons:
                dispatch_result = None
            elif operator_recovery_request_container_path:
                try:
                    active_runner_request_write = await run_in_threadpool(
                        _write_missionos_auto_operator_recovery_request_to_container,
                        container_path=operator_recovery_request_container_path,
                        request_payload=operator_recovery_request,
                    )
                except (
                    OSError,
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                    ValueError,
                ) as exc:
                    dispatch_result = None
                    blocked_reasons.append(
                        "operator_recovery_request_queue_failed:" + str(exc)
                    )
                else:
                    dispatch_result = None
            elif recovery_action not in MISSIONOS_RUNTIME_RECOVERY_EMERGENCY_ACTIONS:
                dispatch_result = None
                blocked_reasons.append("recovery_action_requires_active_runner")
            else:
                try:
                    dispatch_result = await run_in_threadpool(
                        run_px4_gazebo_emergency_command_dispatch,
                        recovery_action=recovery_action,
                        approval=approval,
                        allowlist=allowlist,
                        endpoint_host=str(
                            body.get("endpoint_host") or "127.0.0.1"
                        ),
                        endpoint_port=int(body.get("endpoint_port") or 18570),
                        live_mavlink_opt_in=True,
                    )
                except (
                    PX4GazeboEmergencyDispatcherError,
                    ValidationError,
                    OSError,
                ) as exc:
                    dispatch_result = None
                    blocked_reasons.append(str(exc))

            dispatch_payload = (
                dispatch_result.model_dump(mode="json") if dispatch_result is not None else {}
            )
            if dispatch_result is not None:
                blocked_reasons.extend(str(item) for item in dispatch_result.blocked_reasons)
            if active_runner_request_write is not None and not blocked_reasons:
                dispatch_status = "queued_for_active_runner"
            else:
                dispatch_status = (
                    str(dispatch_result.dispatch_status.value)
                    if dispatch_result is not None
                    else "blocked"
                )
            runner_abort_observed = False
            receipt = {
                "schema_version": "missionos_runtime_recovery_dispatch_receipt.v1",
                "task_id": task_id,
                "dispatch_status": dispatch_status,
                "recovery_action": recovery_action,
                "recovery_parameters": recovery_parameters,
                "operator_approved": True,
                "explicit_recovery_dispatch_approval": True,
                "emergency_command_approval": (
                    _approval_json(approval)
                    if recovery_action in MISSIONOS_RUNTIME_RECOVERY_EMERGENCY_ACTIONS
                    else {}
                ),
                "emergency_command_allowlist": (
                    _approval_json(allowlist)
                    if recovery_action in MISSIONOS_RUNTIME_RECOVERY_EMERGENCY_ACTIONS
                    else {}
                ),
                "maneuver_approval": (
                    _approval_json(approval)
                    if recovery_action in MISSIONOS_RUNTIME_RECOVERY_MANEUVER_ACTIONS
                    else {}
                ),
                "maneuver_allowlist": (
                    _approval_json(allowlist)
                    if recovery_action in MISSIONOS_RUNTIME_RECOVERY_MANEUVER_ACTIONS
                    else {}
                ),
                "emergency_command_dispatch_result": dispatch_payload,
                "active_runner_request_queued": active_runner_request_write is not None,
                "active_runner_request_write": active_runner_request_write,
                "operator_recovery_request": (
                    operator_recovery_request
                    if active_runner_request_write is not None
                    else {}
                ),
                "runner_abort_observed": runner_abort_observed,
                "blocked_reasons": blocked_reasons,
                "delivery_completion_claimed": False,
                "progress_counted": False,
                "physical_execution_invoked": False,
                "hardware_target_allowed": False,
                "observed_at": now.isoformat(),
            }
            artifact_updates: dict[str, Any] = {
                "missionos_runtime_recovery_dispatch_receipt": receipt
            }
            if active_runner_request_write is not None:
                artifact_updates["missionos_runtime_recovery_dispatch_request"] = (
                    operator_recovery_request
                )
            updated_task = self.task_store.update(
                task_id,
                artifacts=artifact_updates,
                metadata={
                    "missionos_runtime_recovery_dispatch_status": dispatch_status,
                    "missionos_runtime_recovery_action": recovery_action,
                    "missionos_runtime_recovery_active_runner_request_queued": (
                        active_runner_request_write is not None
                    ),
                    "missionos_runtime_recovery_runner_abort_observed": runner_abort_observed,
                    "delivery_completion_claimed": False,
                    "physical_execution_invoked": False,
                    "hardware_target_allowed": False,
                },
            )
            summary = {
                "task_id": task_id,
                "task_status": (updated_task or task).get("status"),
                "dispatch_status": dispatch_status,
                "recovery_action": recovery_action,
                "recovery_parameters": recovery_parameters,
                "command_ack_observed": (
                    None
                    if dispatch_result is None
                    else dispatch_result.command_ack_observed
                ),
                "command_ack_result_name": (
                    None
                    if dispatch_result is None
                    else dispatch_result.command_ack_result_name
                ),
                "active_runner_request_queued": active_runner_request_write is not None,
                "runner_abort_observed": runner_abort_observed,
                "blocked_reasons": blocked_reasons,
                "delivery_completion_claimed": False,
                "progress_counted": False,
                "physical_execution_invoked": False,
                "hardware_target_allowed": False,
            }
            status_code = (
                200
                if (
                    active_runner_request_write is not None
                    or (
                        dispatch_result is not None
                        and dispatch_result.dispatch_status
                        == PX4GazeboEmergencyCommandDispatchStatus.ACCEPTED
                    )
                )
                else 409
            )
            return JSONResponse(
                status_code=status_code,
                content={
                    "response_status": dispatch_status,
                    "task": updated_task or task,
                    "missionos_runtime_recovery_dispatch_receipt": receipt,
                    "summary": summary,
                },
            )

        @self.app.post("/px4-gazebo/mission-scenarios/execute-sitl")
        async def px4_gazebo_mission_scenario_execute_sitl(
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            task_id = str(body.get("task_id") or "").strip()
            if not task_id:
                raise HTTPException(status_code=400, detail="task_id is required")
            if body.get("explicit_execution_approval") is not True:
                raise HTTPException(
                    status_code=400,
                    detail="explicit_execution_approval is required",
                )
            task = self.task_store.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            artifacts = task.get("artifacts") or {}
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            metadata = task.get("metadata") or {}
            metadata = metadata if isinstance(metadata, dict) else {}
            operator_route_requested = _mission_designer_operator_route_requested(
                artifacts=artifacts,
                metadata=metadata,
            )
            route_bound_to_sitl = _mission_designer_route_bound_to_sitl(artifacts)
            if operator_route_requested and not route_bound_to_sitl:
                blocked_reasons = [
                    _MISSION_DESIGNER_COORDINATE_ROUTE_BLOCKED_REASON,
                    "current_safe_route_sitl_not_bound_to_operator_route",
                ]
                blocked_task = self.task_store.update(
                    task_id,
                    status="blocked",
                    artifacts={
                        "mission_designer_sitl_route_binding_block": {
                            "schema_version": "mission_designer_sitl_route_binding_block.v1",
                            "operator_route_requested": True,
                            "operator_route_bound_to_sitl": False,
                            "blocked_reasons": blocked_reasons,
                            "gazebo_execution_invoked": False,
                            "mavlink_dispatch_performed": False,
                            "px4_mission_upload_performed": False,
                            "hardware_target_allowed": False,
                            "physical_execution_invoked": False,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    },
                    metadata={
                        "operator_route_requested": True,
                        "operator_route_bound_to_sitl": False,
                        "operator_route_blocked_reason": _MISSION_DESIGNER_COORDINATE_ROUTE_BLOCKED_REASON,
                    },
                )
                return JSONResponse(
                    status_code=409,
                    content={
                        "sitl_execution_opted_in": mission_designer_sitl_execution_opted_in(),
                        "live_flight_mode_requested": body.get("live_flight_mode")
                        is True,
                        "live_flight_opted_in": mission_designer_live_sitl_flight_opted_in(),
                        "task": blocked_task or task,
                        "mission_designer_sitl_route_binding_block": (
                            (blocked_task or task)
                            .get("artifacts", {})
                            .get("mission_designer_sitl_route_binding_block", {})
                        ),
                        "summary": {
                            "task_id": task_id,
                            "task_status": "blocked",
                            "upload_status": "blocked",
                            "operator_route_requested": True,
                            "operator_route_bound_to_sitl": False,
                            "blocked_reasons": blocked_reasons,
                            "gazebo_execution_invoked": False,
                            "external_dispatch_performed": False,
                            "mavlink_dispatch_performed": False,
                            "px4_mission_upload_performed": False,
                            "hardware_target_allowed": False,
                            "physical_execution_invoked": False,
                        },
                    },
                )
            live_flight_mode = body.get("live_flight_mode") is True
            opted_in = mission_designer_sitl_execution_opted_in()
            live_flight_opted_in = mission_designer_live_sitl_flight_opted_in()
            auto_mission_gui_dispatch_requested = (
                live_flight_mode
                and isinstance(
                    artifacts.get("mission_designer_coordinate_pair_route"), Mapping
                )
                and route_bound_to_sitl
            )
            auto_mission_gui_dispatch_opted_in = (
                missionos_auto_mission_gui_dispatch_opted_in()
            )
            if live_flight_mode and (not opted_in or not live_flight_opted_in):
                blocked_reasons: list[str] = []
                if not opted_in:
                    blocked_reasons.append(
                        "Mission Designer SITL execution requires explicit opt-in"
                    )
                if not live_flight_opted_in:
                    blocked_reasons.append(
                        "Mission Designer live SITL flight requires explicit opt-in"
                    )
                try:
                    blocked = attach_px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt(
                        task_id,
                        sitl_execution_opted_in=opted_in,
                        live_flight_opted_in=live_flight_opted_in,
                        blocked_reasons=blocked_reasons,
                        task_store_factory=lambda: self.task_store,
                    )
                except (
                    MissionOSAutoMissionRunnerError,
                    PX4GazeboMissionDesignerSITLLiveFlightRunError,
                    ValidationError,
                ) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                return JSONResponse(
                    status_code=409,
                    content={
                        "sitl_execution_opted_in": opted_in,
                        "live_flight_mode_requested": True,
                        "live_flight_opted_in": live_flight_opted_in,
                        "task": blocked["task"],
                        "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt": blocked[
                            "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt"
                        ],
                        "summary": blocked["summary"],
                    },
                )
            if (
                auto_mission_gui_dispatch_requested
                and not auto_mission_gui_dispatch_opted_in
            ):
                blocked_reasons = [
                    "MissionOS AUTO mission GUI dispatch requires explicit opt-in",
                ]
                blocked_task = self.task_store.update(
                    task_id,
                    status="blocked",
                    artifacts={
                        "missionos_auto_mission_gui_dispatch_blocked_receipt": {
                            "schema_version": "missionos_auto_mission_gui_dispatch_blocked_receipt.v1",
                            "task_id": task_id,
                            "dispatch_status": "blocked",
                            "blocked_reasons": blocked_reasons,
                            "sitl_execution_opted_in": opted_in,
                            "live_flight_opted_in": live_flight_opted_in,
                            "auto_mission_gui_dispatch_requested": True,
                            "auto_mission_gui_dispatch_opted_in": False,
                            "auto_mission_gui_dispatch_opt_in_env": MISSIONOS_AUTO_MISSION_GUI_DISPATCH_OPT_IN_ENV,
                            "live_flight_runner_invoked": False,
                            "actual_sitl_flight_evidence_observed": False,
                            "external_dispatch_performed": False,
                            "mavlink_dispatch_performed": False,
                            "px4_mission_upload_performed": False,
                            "hardware_target_allowed": False,
                            "physical_execution_invoked": False,
                            "delivery_completion_claimed": False,
                            "physical_delivery_verified": False,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    },
                    metadata={
                        "missionos_auto_mission_gui_dispatch_requested": True,
                        "missionos_auto_mission_gui_dispatch_status": "blocked",
                        "missionos_auto_mission_gui_dispatch_blocked_reason": (
                            "auto_mission_gui_dispatch_opt_in_missing"
                        ),
                        "hardware_target_allowed": False,
                        "physical_execution_invoked": False,
                        "delivery_completion_claimed": False,
                        "physical_delivery_verified": False,
                    },
                )
                return JSONResponse(
                    status_code=409,
                    content={
                        "sitl_execution_opted_in": opted_in,
                        "live_flight_mode_requested": True,
                        "live_flight_opted_in": live_flight_opted_in,
                        "auto_mission_gui_dispatch_requested": True,
                        "auto_mission_gui_dispatch_opted_in": False,
                        "task": blocked_task or task,
                        "missionos_auto_mission_gui_dispatch_blocked_receipt": (
                            (blocked_task or task)
                            .get("artifacts", {})
                            .get(
                                "missionos_auto_mission_gui_dispatch_blocked_receipt",
                                {},
                            )
                        ),
                        "summary": {
                            "task_id": task_id,
                            "task_status": "blocked",
                            "upload_status": "blocked",
                            "live_flight_status": "blocked",
                            "live_flight_mode_requested": True,
                            "live_flight_opted_in": live_flight_opted_in,
                            "auto_mission_gui_dispatch_requested": True,
                            "auto_mission_gui_dispatch_opted_in": False,
                            "live_flight_runner_invoked": False,
                            "actual_sitl_flight_evidence_observed": False,
                            "blocked_reasons": blocked_reasons,
                            "gazebo_execution_invoked": False,
                            "external_dispatch_performed": False,
                            "mavlink_dispatch_performed": False,
                            "px4_mission_upload_performed": False,
                            "hardware_target_allowed": False,
                            "physical_execution_invoked": False,
                            "delivery_completion_claimed": False,
                            "physical_delivery_verified": False,
                        },
                    },
                )
            if live_flight_mode:
                readiness = await run_in_threadpool(
                    build_px4_gazebo_sitl_execution_readiness,
                    endpoint_host="127.0.0.1",
                    mavlink_udp_port=14540,
                    docker_required=True,
                    timeout_seconds=2.0,
                    sitl_startup_action_available=(
                        _mission_designer_sitl_startup_action_available()
                    ),
                )
                if readiness.get("readiness_status") != "ready":
                    return _mission_designer_sitl_readiness_response(
                        task_id=task_id,
                        task=task,
                        readiness=readiness,
                        status_code=409,
                    )
            if (
                auto_mission_gui_dispatch_requested
                and envelope_violation_advisory_requested(
                    build_envelope_violation_advisory(artifacts=artifacts)
                )
            ):
                auto_mission_gui_dispatch_requested = False
            if auto_mission_gui_dispatch_requested:
                try:
                    auto_result = await run_in_threadpool(
                        run_missionos_auto_mission_gui_dispatch_execution,
                        task_id,
                        task_store_factory=lambda: self.task_store,
                    )
                except (
                    MissionOSAutoMissionRunnerError,
                    PX4GazeboMissionDesignerSITLLiveFlightRunError,
                    ValidationError,
                ) as exc:
                    blocked_reasons = [
                        "missionos_auto_mission_gui_dispatch_failed",
                        str(exc),
                    ]
                    failed_task = self.task_store.update(
                        task_id,
                        status="blocked",
                        artifacts={
                            "missionos_auto_mission_gui_dispatch_failed_receipt": {
                                "schema_version": "missionos_auto_mission_gui_dispatch_failed_receipt.v1",
                                "task_id": task_id,
                                "dispatch_status": "blocked",
                                "failure_reason": str(exc),
                                "blocked_reasons": blocked_reasons,
                                "sitl_execution_opted_in": opted_in,
                                "live_flight_opted_in": live_flight_opted_in,
                                "auto_mission_gui_dispatch_requested": True,
                                "auto_mission_gui_dispatch_opted_in": True,
                                "actual_sitl_flight_evidence_observed": False,
                                "hardware_target_allowed": False,
                                "physical_execution_invoked": False,
                                "delivery_completion_claimed": False,
                                "physical_delivery_verified": False,
                                "observed_at": datetime.now(timezone.utc).isoformat(),
                            }
                        },
                        metadata={
                            "missionos_auto_mission_gui_dispatch_requested": True,
                            "missionos_auto_mission_gui_dispatch_status": "blocked",
                            "missionos_auto_mission_gui_dispatch_failure_reason": str(
                                exc
                            ),
                            "actual_sitl_flight_evidence_observed": False,
                            "hardware_target_allowed": False,
                            "physical_execution_invoked": False,
                            "delivery_completion_claimed": False,
                            "physical_delivery_verified": False,
                        },
                    )
                    return JSONResponse(
                        status_code=409,
                        content={
                            "sitl_execution_opted_in": opted_in,
                            "live_flight_mode_requested": True,
                            "live_flight_opted_in": live_flight_opted_in,
                            "auto_mission_gui_dispatch_requested": True,
                            "auto_mission_gui_dispatch_opted_in": True,
                            "task": failed_task or task,
                            "missionos_auto_mission_gui_dispatch_failed_receipt": (
                                (failed_task or task)
                                .get("artifacts", {})
                                .get(
                                    "missionos_auto_mission_gui_dispatch_failed_receipt",
                                    {},
                                )
                            ),
                            "summary": {
                                "task_id": task_id,
                                "task_status": "blocked",
                                "upload_status": "blocked",
                                "live_flight_status": "blocked",
                                "auto_mission_gui_dispatch_requested": True,
                                "auto_mission_gui_dispatch_opted_in": True,
                                "actual_sitl_flight_evidence_observed": False,
                                "blocked_reasons": blocked_reasons,
                                "hardware_target_allowed": False,
                                "physical_execution_invoked": False,
                                "delivery_completion_claimed": False,
                                "physical_delivery_verified": False,
                            },
                        },
                    )
                return {
                    "sitl_execution_opted_in": opted_in,
                    "live_flight_mode_requested": True,
                    "live_flight_opted_in": live_flight_opted_in,
                    "auto_mission_gui_dispatch_requested": True,
                    "auto_mission_gui_dispatch_opted_in": True,
                    **auto_result,
                    "summary": {
                        **auto_result["summary"],
                        "live_flight_mode_requested": True,
                        "live_flight_opted_in": live_flight_opted_in,
                        "auto_mission_gui_dispatch_requested": True,
                        "auto_mission_gui_dispatch_opted_in": True,
                    },
                }
            envelope_advisory = build_envelope_violation_advisory(
                artifacts=artifacts,
            )
            if envelope_violation_advisory_requested(envelope_advisory):
                blocked_reasons = list(envelope_advisory.get("blocked_reasons") or [])
                blocked_task = self.task_store.update(
                    task_id,
                    status="blocked",
                    artifacts={
                        "envelope_violation_advisory": envelope_advisory,
                    },
                    metadata={
                        "mission_designer_envelope_violation_observed": True,
                        "mission_designer_envelope_violation_blocked_reasons": blocked_reasons,
                        "mission_designer_sitl_upload_blocked_by_envelope_advisory": True,
                        "hardware_target_allowed": False,
                        "physical_execution_invoked": False,
                    },
                )
                return JSONResponse(
                    status_code=409,
                    content={
                        "sitl_execution_opted_in": opted_in,
                        "live_flight_mode_requested": live_flight_mode,
                        "live_flight_opted_in": live_flight_opted_in,
                        "task": blocked_task or task,
                        "envelope_violation_advisory": envelope_advisory,
                        "summary": {
                            "task_id": task_id,
                            "task_status": "blocked",
                            "result_status": "blocked",
                            "upload_status": "blocked",
                            "operator_route_requested": operator_route_requested,
                            "operator_route_bound_to_sitl": route_bound_to_sitl,
                            "envelope_violation_observed": True,
                            "operator_review_required": True,
                            "automatic_dispatch_suppressed": True,
                            "blocked_reasons": blocked_reasons,
                            "gazebo_execution_invoked": False,
                            "external_dispatch_performed": False,
                            "mavlink_dispatch_performed": False,
                            "px4_mission_upload_performed": False,
                            "hardware_target_allowed": False,
                            "physical_execution_invoked": False,
                            "delivery_completion_claimed": False,
                            "payload_dropoff_success_claimed": False,
                        },
                    },
                )
            try:
                result = await run_in_threadpool(
                    run_px4_gazebo_mission_designer_sitl_execution,
                    task_id,
                    explicit_execution_approval=True,
                    allow_sitl_execution=opted_in,
                )
            except (PX4GazeboMissionDesignerSITLRunnerError, ValidationError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            live_result = None
            if live_flight_mode:
                upload_status = str(result["summary"].get("upload_status") or "")
                upload_receipt = result["px4_gazebo_sitl_mission_upload_receipt"]
                try:
                    mission_request_sequences = tuple(
                        int(item)
                        for item in (
                            upload_receipt.get("mission_request_sequences") or ()
                        )
                    )
                except (TypeError, ValueError):
                    mission_request_sequences = ()
                live_upload_blocked_reasons = []
                if upload_status != "uploaded":
                    live_upload_blocked_reasons.append(
                        "Mission Designer live SITL flight requires uploaded mission receipt"
                    )
                if (
                    upload_receipt.get("mission_ack_observed") is not True
                    or upload_receipt.get("mission_ack_type") != MAV_MISSION_ACCEPTED
                ):
                    live_upload_blocked_reasons.append(
                        "Mission Designer live SITL flight requires accepted mission ACK"
                    )
                if mission_request_sequences != (0, 1, 2, 3):
                    live_upload_blocked_reasons.append(
                        "Mission Designer live SITL flight requires complete mission request sequence"
                    )
                if live_upload_blocked_reasons:
                    blocked_reasons = [
                        *live_upload_blocked_reasons,
                        *[
                            str(item)
                            for item in (
                                result["summary"].get("blocked_reasons") or ()
                            )
                        ],
                    ]
                    try:
                        blocked = attach_px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt(
                            task_id,
                            sitl_execution_opted_in=opted_in,
                            live_flight_opted_in=live_flight_opted_in,
                            blocked_reasons=blocked_reasons,
                            task_store_factory=lambda: self.task_store,
                        )
                    except (
                        PX4GazeboMissionDesignerSITLLiveFlightRunError,
                        ValidationError,
                    ) as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                    response = {
                        "sitl_execution_opted_in": opted_in,
                        "live_flight_mode_requested": True,
                        "live_flight_opted_in": live_flight_opted_in,
                        "task": blocked["task"],
                        "delivery_mission_contract": result[
                            "delivery_mission_contract"
                        ],
                        "simulated_command_proposal": result[
                            "simulated_command_proposal"
                        ],
                        "simulated_command_approval": result[
                            "simulated_command_approval"
                        ],
                        "simulator_command_execution_preflight": result[
                            "simulator_command_execution_preflight"
                        ],
                        "px4_gazebo_sitl_mission_upload_receipt": result[
                            "px4_gazebo_sitl_mission_upload_receipt"
                        ],
                        "px4_gazebo_mission_designer_sitl_execution_result": result[
                            "px4_gazebo_mission_designer_sitl_execution_result"
                        ],
                        "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt": blocked[
                            "px4_gazebo_mission_designer_sitl_live_flight_blocked_receipt"
                        ],
                        **{
                            key: result[key]
                            for key in (
                                "environment_condition_profile",
                                "simulator_capability_matrix",
                                "simulator_condition_application",
                                "observed_environment_evidence",
                            )
                            if key in result
                        },
                        "summary": {
                            **result["summary"],
                            **blocked["summary"],
                            "upload_status": upload_status,
                            "live_flight_mode_requested": True,
                            "live_flight_opted_in": live_flight_opted_in,
                        },
                    }
                    return JSONResponse(status_code=409, content=response)
                try:
                    live_result = await run_in_threadpool(
                        run_px4_gazebo_mission_designer_live_sitl_flight_execution,
                        task_id,
                        task_store_factory=lambda: self.task_store,
                    )
                except (
                    PX4GazeboMissionDesignerSITLLiveFlightRunError,
                    ValidationError,
                ) as exc:
                    try:
                        execution_result = result[
                            "px4_gazebo_mission_designer_sitl_execution_result"
                        ]
                        failed = attach_px4_gazebo_mission_designer_sitl_live_flight_failed_receipt(
                            task_id,
                            sitl_execution_opted_in=opted_in,
                            live_flight_opted_in=live_flight_opted_in,
                            mission_upload_observed=(
                                execution_result.get("mission_upload_observed")
                                is True
                            ),
                            mission_ack_observed=(
                                upload_receipt.get("mission_ack_observed") is True
                            ),
                            mission_ack_type=upload_receipt.get("mission_ack_type"),
                            external_dispatch_performed=(
                                execution_result.get("external_dispatch_performed")
                                is True
                            ),
                            mavlink_dispatch_performed=(
                                execution_result.get("mavlink_dispatch_performed")
                                is True
                            ),
                            px4_mission_upload_performed=(
                                execution_result.get("px4_mission_upload_performed")
                                is True
                            ),
                            failure_message=str(exc),
                            task_store_factory=lambda: self.task_store,
                        )
                    except (
                        PX4GazeboMissionDesignerSITLLiveFlightRunError,
                        ValidationError,
                    ) as attach_exc:
                        raise HTTPException(
                            status_code=400, detail=str(attach_exc)
                        ) from attach_exc
                    response = {
                        "sitl_execution_opted_in": opted_in,
                        "live_flight_mode_requested": True,
                        "live_flight_opted_in": live_flight_opted_in,
                        "task": failed["task"],
                        "delivery_mission_contract": result[
                            "delivery_mission_contract"
                        ],
                        "simulated_command_proposal": result[
                            "simulated_command_proposal"
                        ],
                        "simulated_command_approval": result[
                            "simulated_command_approval"
                        ],
                        "simulator_command_execution_preflight": result[
                            "simulator_command_execution_preflight"
                        ],
                        "px4_gazebo_sitl_mission_upload_receipt": result[
                            "px4_gazebo_sitl_mission_upload_receipt"
                        ],
                        "px4_gazebo_mission_designer_sitl_execution_result": result[
                            "px4_gazebo_mission_designer_sitl_execution_result"
                        ],
                        "px4_gazebo_mission_designer_sitl_live_flight_failed_receipt": failed[
                            "px4_gazebo_mission_designer_sitl_live_flight_failed_receipt"
                        ],
                        **{
                            key: result[key]
                            for key in (
                                "environment_condition_profile",
                                "simulator_capability_matrix",
                                "simulator_condition_application",
                                "observed_environment_evidence",
                            )
                            if key in result
                        },
                        "summary": {
                            **result["summary"],
                            **failed["summary"],
                            "upload_status": upload_status,
                            "live_flight_mode_requested": True,
                            "live_flight_opted_in": live_flight_opted_in,
                        },
                    }
                    return JSONResponse(status_code=409, content=response)

            response = {
                "sitl_execution_opted_in": opted_in,
                "live_flight_mode_requested": live_flight_mode,
                "live_flight_opted_in": live_flight_opted_in,
                "task": result["task"],
                "delivery_mission_contract": result["delivery_mission_contract"],
                "simulated_command_proposal": result["simulated_command_proposal"],
                "simulated_command_approval": result["simulated_command_approval"],
                "simulator_command_execution_preflight": result[
                    "simulator_command_execution_preflight"
                ],
                "px4_gazebo_sitl_mission_upload_receipt": result[
                    "px4_gazebo_sitl_mission_upload_receipt"
                ],
                "px4_gazebo_mission_designer_sitl_execution_result": result[
                    "px4_gazebo_mission_designer_sitl_execution_result"
                ],
                **{
                    key: result[key]
                    for key in (
                        "environment_condition_profile",
                        "simulator_capability_matrix",
                        "simulator_condition_application",
                        "observed_environment_evidence",
                    )
                    if key in result
                },
                "summary": result["summary"],
            }
            if live_result is not None:
                response.update(
                    {
                        "task": live_result["task"],
                        "px4_gazebo_mission_designer_sitl_live_flight_run": live_result[
                            "px4_gazebo_mission_designer_sitl_live_flight_run"
                        ],
                        **{
                            key: live_result[key]
                            for key in (
                                "environment_condition_profile",
                                "simulator_capability_matrix",
                                "simulator_condition_application",
                                "observed_environment_evidence",
                            )
                            if key in live_result
                        },
                        "summary": {
                            **result["summary"],
                            **live_result["summary"],
                            "upload_status": result["summary"]["upload_status"],
                            "live_flight_mode_requested": True,
                            "live_flight_opted_in": live_flight_opted_in,
                        },
                    }
                )
            if not opted_in:
                return JSONResponse(status_code=409, content=response)
            return response

        # --- sessions ---

        @self.app.get("/sessions/{user_id}")
        async def list_sessions(user_id: str, request: Request):
            effective_user_id = self._resolve_http_user_id(
                request, user_id, default_user_id="api_user"
            )
            sessions = self.transcript.list_sessions(user_id=effective_user_id)
            if not sessions:
                response = await self.session_service.list_sessions(
                    app_name="boiled-claw", user_id=effective_user_id
                )
                hydrated = response.sessions or []
                return {
                    "sessions": [
                        {
                            "id": s.id,
                            "user_id": effective_user_id,
                            "last_activity": getattr(s, "last_update_time", 0.0),
                            "preview": "",
                            "entry_count": len(getattr(s, "events", []) or []),
                        }
                        for s in hydrated
                    ]
                }

            return {
                "sessions": [
                    {
                        "id": item["session_id"],
                        "user_id": item["user_id"],
                        "last_activity": item["last_activity"],
                        "preview": item["preview"],
                        "entry_count": item["entry_count"],
                    }
                    for item in sessions
                ]
            }

        # --- transcript / history ---

        @self.app.get("/sessions/{user_id}/{session_id}/history")
        async def get_session_history(
            request: Request,
            user_id: str,
            session_id: str,
            limit: int = Query(default=100, ge=1, le=500),
            before: Optional[float] = Query(default=None),
        ):
            effective_user_id = self._resolve_http_user_id(
                request, user_id, default_user_id="api_user"
            )
            if not self.transcript.has_session(session_id, effective_user_id):
                raise HTTPException(status_code=404, detail="session not found")
            entries = self.transcript.get_history(session_id, limit=limit, before=before)
            return {
                "session_id": session_id,
                "entries": [e.to_dict() for e in entries],
                "count": len(entries),
            }

        @self.app.get("/transcript/sessions")
        async def list_transcript_sessions(
            request: Request,
            user_id: str = Query(...),
            limit: int = Query(default=50, ge=1, le=200),
        ):
            effective_user_id = self._resolve_http_user_id(
                request, user_id, default_user_id="api_user"
            )
            return {
                "sessions": self.transcript.list_sessions(
                    user_id=effective_user_id,
                    limit=limit,
                )
            }

        # --- agent/run (HTTP) ---

        @self.app.post("/agent/run")
        async def run_agent_endpoint(
            request: Request,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            requested_user_id = str(body.get("user_id") or "api_user")
            user_id = self._resolve_http_user_id(
                request,
                requested_user_id,
                default_user_id="api_user",
            )
            message = str(body.get("message") or "").strip()
            session_id = body.get("session_id")

            if not message:
                raise HTTPException(status_code=400, detail="message is required")

            session = await self._get_or_create_gateway_session(
                user_id=user_id,
                session_id=str(session_id) if session_id else None,
            )

            # Record user message in transcript
            self.transcript.append(session.id, "user", message, user_id=user_id)

            result = await self._run_agent_http(user_id, session.id, message)

            # Record assistant response in transcript
            self.transcript.append(
                session.id, "assistant", result["message"],
                user_id=user_id,
                metadata={"type": result["type"]},
            )

            return {
                "ok": result.get("ok", result["type"] == "agent_message"),
                "type": result["type"],
                "response": result["message"],
                "user_id": user_id,
                "session_id": session.id,
            }

        @self.app.post("/control-loop/run")
        async def run_control_loop_endpoint(
            request: Request,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            requested_user_id = str(body.get("user_id") or "api_user")
            user_id = self._resolve_http_user_id(
                request,
                requested_user_id,
                default_user_id="api_user",
            )
            goal = str(body.get("goal") or "").strip()
            session_id = body.get("session_id")
            constraints = normalize_constraints(body.get("constraints"))

            if not goal:
                raise HTTPException(status_code=400, detail="goal is required")

            session = await self._get_or_create_gateway_session(
                user_id=user_id,
                session_id=str(session_id) if session_id else None,
            )
            self.transcript.append(
                session.id,
                "user",
                goal,
                user_id=user_id,
                metadata={"type": "control_loop", "constraints": constraints},
            )

            result = await self._run_control_loop_http(
                user_id=user_id,
                session_id=session.id,
                goal=goal,
                constraints=constraints,
                source="http",
                reset_if_terminal=True,
            )
            self.transcript.append(
                session.id,
                "assistant",
                result.final_text,
                user_id=user_id,
                metadata={
                    "type": "control_loop",
                    "success": result.success,
                    "needs_human": bool(result.metadata.get("needs_human")),
                    "plan_id": result.plan_id,
                    "task_id": result.metadata.get("task_id"),
                },
            )
            if result.metadata.get("needs_human"):
                await self._emit_control_approval_request(
                    session.id,
                    result.metadata.get("approval_request"),
                )

            return {
                "ok": result.success,
                "response": result.final_text,
                "user_id": user_id,
                "session_id": session.id,
                "plan_id": result.plan_id,
                "verification_report_id": result.verification_report_id,
                "repair_count": result.repair_count,
                "task_id": result.metadata.get("task_id"),
                "needs_human": bool(result.metadata.get("needs_human")),
                "approval_request": result.metadata.get("approval_request"),
                "promoted_memory_ids": result.promoted_memory_ids,
            }

        @self.app.post("/control-loop/approve")
        async def approve_control_loop_endpoint(
            request: Request,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            requested_user_id = str(body.get("user_id") or "api_user")
            user_id = self._resolve_http_user_id(
                request,
                requested_user_id,
                default_user_id="api_user",
            )
            session_id = str(body.get("session_id") or "").strip()
            request_id = str(body.get("request_id") or "").strip()
            approved = bool(body.get("approved", False))

            if not session_id or not request_id:
                raise HTTPException(
                    status_code=400,
                    detail="session_id and request_id are required",
                )

            pending = await self.control_loop.get_pending_approval(
                user_id=user_id,
                session_id=session_id,
            )
            pending_task_id = self._find_control_loop_task_for_approval(
                session_id=session_id,
                request_id=request_id,
            )
            approval_owner_user_id = self._find_control_loop_task_owner_for_approval(
                session_id=session_id,
                request_id=request_id,
            )
            resolved = await self.control_loop.resolve_human_approval(
                user_id=user_id,
                session_id=session_id,
                approved=approved,
                request_id=request_id,
            )
            if (
                not pending
                and not resolved
                and approval_owner_user_id
                and approval_owner_user_id != user_id
            ):
                user_id = approval_owner_user_id
                pending = await self.control_loop.get_pending_approval(
                    user_id=user_id,
                    session_id=session_id,
                )
                resolved = await self.control_loop.resolve_human_approval(
                    user_id=user_id,
                    session_id=session_id,
                    approved=approved,
                    request_id=request_id,
                )
            if not resolved:
                raise HTTPException(status_code=404, detail="approval request not found")

            if approved and pending:
                resume_goal = (
                    await self.control_loop.get_task_goal(
                        user_id=user_id,
                        session_id=session_id,
                    )
                    or pending.get("goal", "")
                )
                await self._start_control_loop_run(
                    user_id=user_id,
                    session_id=session_id,
                    goal=resume_goal,
                    constraints=normalize_constraints(
                        (pending.get("plan") or {}).get("constraints")
                    ),
                    task_id=pending_task_id,
                )

            response = {
                "ok": True,
                "session_id": session_id,
                "request_id": request_id,
                "approved": approved,
            }
            if approved and pending:
                response["result"] = {
                    "ok": True,
                    "response": "Approval accepted. Control loop resumed in background.",
                    "task_id": pending_task_id,
                    "needs_human": False,
                }
            return response

        # --- memory ---

        @self.app.get("/memory/stats")
        async def memory_stats_endpoint():
            return await memory_stats()

        @self.app.get("/memory")
        async def memory_search_endpoint(
            query: Optional[str] = None,
            tags: Optional[str] = None,
            limit: int = 10,
        ):
            return await memory_search(query=query, tags=tags, limit=limit)

        @self.app.delete("/memory/{memory_id}")
        async def memory_delete_endpoint(memory_id: int):
            result = await memory_delete(memory_id)
            if not result.get("success"):
                raise HTTPException(status_code=500, detail=result.get("error", "Delete failed"))
            if not result.get("deleted"):
                raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
            return result

        # --- subagents ---

        @self.app.get("/subagents/{session_id}")
        async def subagents_list_endpoint(session_id: str):
            return await self.subagent_manager.list_runs(requester_session_id=session_id)

        self.app.include_router(build_task_router(self))
        self.app.include_router(build_audit_router(self))

        @self.app.post("/subagents/{run_id}/steer")
        async def subagents_steer_endpoint(run_id: str, payload: Dict[str, Any] | None = Body(default=None)):
            message = ""
            if payload and isinstance(payload.get("message"), str):
                message = payload.get("message", "").strip()
            if not message:
                raise HTTPException(status_code=400, detail="message is required")
            result = await self.subagent_manager.steer(run_id=run_id, message=message)
            if not result.get("success"):
                raise HTTPException(status_code=400, detail=result.get("error", "steer failed"))
            return result

        @self.app.delete("/subagents/{run_id}")
        async def subagents_kill_endpoint(run_id: str):
            result = await self.subagent_manager.kill(run_id=run_id)
            if not result.get("success"):
                raise HTTPException(status_code=400, detail=result.get("error", "kill failed"))
            return result

        # --- cron ---

        @self.app.get("/cron")
        async def cron_list():
            return {"jobs": [j.to_dict() for j in get_scheduler().list_jobs()]}

        @self.app.post("/cron")
        async def cron_create(payload: Dict[str, Any] | None = Body(default=None)):
            body = payload or {}
            try:
                job = get_scheduler().add_job(
                    name=str(body.get("name") or ""),
                    cron_expr=str(body.get("cron_expr") or ""),
                    task=str(body.get("task") or ""),
                    agent_id=str(body.get("agent_id") or "web_researcher"),
                    delivery_target=self._resolve_cron_delivery_target(body),
                    max_retries=int(body.get("max_retries") or 0),
                    retry_delay=int(body.get("retry_delay") or 30),
                    system_event=body.get("system_event") or None,
                )
                return {"ok": True, "job": job.to_dict()}
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        @self.app.delete("/cron/{job_id}")
        async def cron_delete(job_id: str):
            if not get_scheduler().delete_job(job_id):
                raise HTTPException(status_code=404, detail="job not found")
            return {"ok": True}

        @self.app.patch("/cron/{job_id}")
        async def cron_toggle(job_id: str, payload: Dict[str, Any] | None = Body(default=None)):
            body = payload or {}
            enabled = bool(body.get("enabled", True))
            job = get_scheduler().toggle_job(job_id, enabled)
            if not job:
                raise HTTPException(status_code=404, detail="job not found")
            return {"ok": True, "job": job.to_dict()}

        # --- tool policy ---

        @self.app.get("/tools/policy")
        async def tool_policy_list():
            return self.tool_policy.list_policies()

        @self.app.get("/tools/approvals")
        async def tool_approvals_list(
            session_id: Optional[str] = None,
            state: Optional[str] = None,
            include_expired: bool = False,
            q: Optional[str] = None,
            page: int = 1,
            page_size: Optional[int] = None,
            limit: Optional[int] = None,
        ):
            selected_state = state or "pending"
            resolved_page_size = max(1, min(int(page_size or limit or 20), 100))
            return self.tool_policy.query_approvals(
                session_id=session_id,
                state=selected_state,
                include_expired=include_expired,
                q=q,
                page=page,
                page_size=resolved_page_size,
            )

        @self.app.get("/tools/approvals/{request_id}")
        async def tool_approval_get(request_id: str):
            approval = self.tool_policy.get_approval(request_id)
            if approval is None:
                raise HTTPException(status_code=404, detail=f"approval not found: {request_id}")
            return {
                "approval": approval,
                "resolve_suggestions": self._approval_resolve_suggestions(approval),
            }

        @self.app.post("/tools/approvals/{request_id}/resolve")
        async def tool_approval_resolve(
            request: Request,
            request_id: str,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            if "approved" not in body:
                raise HTTPException(status_code=400, detail="approved is required")
            approved = bool(body.get("approved"))
            reason = str(body.get("reason") or "").strip()
            session_id = str(body.get("session_id") or "")
            user_id = self._resolve_http_user_id(
                request,
                str(body.get("user_id") or "api_user"),
                default_user_id="api_user",
            )
            result = await self._resolve_tool_approval_request(
                request_id=request_id,
                approved=approved,
                reason=reason,
                session_id=session_id,
                user_id=user_id,
                source="http",
                scope=body.get("scope"),
                tool_pattern=body.get("tool_pattern"),
                path_scope=body.get("path_scope"),
                expires_at=body.get("expires_at"),
                propagate_to_subagents=body.get("propagate_to_subagents"),
            )
            if not result.get("resolved"):
                raise HTTPException(status_code=404, detail=result.get("error", "approval not found"))
            return result

        @self.app.post("/tools/approvals/{request_id}/resolve_bundle")
        async def tool_approval_resolve_bundle(
            request: Request,
            request_id: str,
            payload: Dict[str, Any] | None = Body(default=None),
        ):
            body = payload or {}
            if "approved" not in body:
                raise HTTPException(status_code=400, detail="approved is required")
            approval = self.tool_policy.get_approval(request_id)
            if approval is None:
                raise HTTPException(status_code=404, detail=f"approval not found: {request_id}")
            strategy = str(body.get("strategy") or "single").strip() or "single"
            approved = bool(body.get("approved"))
            reason = str(body.get("reason") or "").strip()
            session_id = str(body.get("session_id") or approval.get("session_id") or "")
            user_id = self._resolve_http_user_id(
                request,
                str(body.get("user_id") or "api_user"),
                default_user_id="api_user",
            )
            try:
                specs = self._approval_bundle_specs(
                    approval,
                    strategy=strategy,
                    path_scope=body.get("path_scope") if isinstance(body.get("path_scope"), str) else None,
                    propagate_to_subagents=(
                        bool(body.get("propagate_to_subagents"))
                        if body.get("propagate_to_subagents") is not None
                        else None
                    ),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if not specs:
                raise HTTPException(status_code=404, detail="no approvals matched the requested bundle")

            default_reason = (
                f"{'Approved' if approved else 'Denied'} in Web UI ({strategy})"
            )
            results = []
            for spec in specs:
                resolved = await self._resolve_tool_approval_request(
                    request_id=spec["request_id"],
                    approved=approved,
                    reason=reason or default_reason,
                    session_id=session_id,
                    user_id=user_id,
                    source="http",
                    scope=spec.get("scope"),
                    tool_pattern=spec.get("tool_pattern"),
                    path_scope=spec.get("path_scope"),
                    propagate_to_subagents=spec.get("propagate_to_subagents"),
                )
                if resolved.get("resolved"):
                    results.append(resolved)
            if not results:
                raise HTTPException(status_code=404, detail="approvals not found")
            return {
                "resolved": True,
                "strategy": strategy,
                "approved": approved,
                "resolved_count": len(results),
                "request_ids": [item.get("request_id") for item in results if item.get("request_id")],
                "results": results,
            }

        self.app.include_router(build_websocket_router(self))

    # ------------------------------------------------------------------
    # agent execution
    # ------------------------------------------------------------------

    async def _start_agent_run(
        self,
        session_id: str,
        user_id: str,
        message: str,
        request_id: Optional[str] = None,
    ) -> None:
        """Abort existing task then start a new agent run."""
        await self.manager.abort(session_id)
        await self._desktop_clear_stop(session_id=session_id, user_id=user_id)
        task = asyncio.create_task(
            self._agent_run_task(session_id, user_id, message, request_id),
            name=f"agent:{session_id}",
        )
        self.manager.set_task(session_id, task)

    async def _start_control_loop_run(
        self,
        session_id: str,
        user_id: str,
        goal: str,
        constraints: list[str],
        request_id: Optional[str] = None,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        initial_state: Optional[dict[str, Any]] = None,
        reset_if_terminal: bool = False,
    ) -> None:
        await self.manager.abort(session_id)
        await self._desktop_clear_stop(session_id=session_id, user_id=user_id)
        task = asyncio.create_task(
            self._control_loop_task(
                session_id,
                user_id,
                goal,
                constraints,
                request_id,
                task_id,
                parent_task_id,
                replay_of_task_id,
                compare_to_task_id,
                initial_state,
                reset_if_terminal,
            ),
            name=f"control:{session_id}",
        )
        self.manager.set_task(session_id, task)

    async def _agent_run_task(
        self,
        session_id: str,
        user_id: str,
        message: str,
        request_id: Optional[str] = None,
    ) -> None:
        """Run agent and send chat.done. On abort, persist partial + aborted flag."""
        partial = ""
        try:
            # Stock price shortcut
            if is_direct_stock_price_query(message):
                await self._send_tool_event(
                    session_id,
                    ev_tool_start(
                        tool_name="stock_price",
                        agent_name=root_agent.name,
                        args={"query": message},
                        request_id=request_id,
                    ),
                )
                quote = await stock_price(message)
                await self._send_tool_event(
                    session_id,
                    ev_tool_result(
                        tool_name="stock_price",
                        agent_name=root_agent.name,
                        ok=bool(quote.get("ok")),
                        result=self._summarize_tool_result(quote),
                        request_id=request_id,
                    ),
                )
                if quote.get("ok"):
                    partial = (
                        f"Latest daily data for {quote.get('symbol')}:\n"
                        f"- Date: {quote.get('date')}\n"
                        f"- Open: {quote.get('open')}\n"
                        f"- High: {quote.get('high')}\n"
                        f"- Low: {quote.get('low')}\n"
                        f"- Close: {quote.get('close')}\n"
                        f"- Volume: {quote.get('volume')}"
                    )
                else:
                    partial = quote.get("message", "Could not retrieve stock price data.")

                self.transcript.append(
                    session_id, "assistant", partial,
                    user_id=user_id,
                    request_id=request_id,
                )
                await self.manager.send_json(session_id, ev_chat_done(partial, request_id))
                return

            effective_message = self._resolve_control_loop_goal(
                session_id=session_id,
                goal=message,
            )
            decision = await self._select_route_for_message(
                session_id=session_id,
                user_id=user_id,
                message=effective_message,
                source="chat",
            )
            if decision.target == "control_loop":
                await self._emit_routing_event(
                    session_id,
                    status="selected",
                    message=f"Router selected control loop ({decision.reason or 'multi-step task'}).",
                    user_id=user_id,
                    agent_name="root_workflow",
                )
                await self._control_loop_task(
                    session_id,
                    user_id,
                    effective_message,
                    [],
                    request_id,
                    reset_if_terminal=True,
                )
                return

            if decision.target == "dynamic_agent":
                await self._emit_routing_event(
                    session_id,
                    status="selected",
                    message=(
                        f"Router selected dynamic_agent "
                        f"({decision.reason or 'dedicated task environment'})."
                    ),
                    user_id=user_id,
                    agent_name="dynamic_agent",
                )
                spawn = await self._spawn_dynamic_route(
                    session_id=session_id,
                    user_id=user_id,
                    message=message,
                    decision=decision,
                )
                if spawn.get("status") == "accepted":
                    partial = (
                        "Dynamic agent started.\n"
                        f"- run_id: {spawn.get('run_id')}\n"
                        f"- mode: {decision.dynamic_agent.mode or 'run'}"
                    )
                else:
                    partial = spawn.get("error", "Failed to start dynamic agent.")
                self.transcript.append(
                    session_id, "assistant", partial,
                    user_id=user_id,
                    request_id=request_id,
                )
                await self.manager.send_json(
                    session_id,
                    ev_chat_done(partial, request_id, aborted=False),
                )
                return

            routed_message = message
            if decision.target == "specialist" and decision.specialist:
                await self._emit_routing_event(
                    session_id,
                    status="selected",
                    message=(
                        f"Router selected {decision.specialist} "
                        f"({decision.reason or 'specialized task'})."
                    ),
                    user_id=user_id,
                    agent_name=decision.specialist,
                )
                if not decision.preflight_specialist:
                    prepass = await self._run_specialist_prepass(
                        session_id=session_id,
                        user_id=user_id,
                        message=message,
                        specialist_name=decision.specialist,
                        request_id=request_id,
                    )
                    if prepass.infrastructure_blocked:
                        partial = self._format_specialist_runtime_failure(
                            decision.specialist,
                            prepass,
                        )
                    else:
                        partial = prepass.text
                    if not partial.strip():
                        partial = "Specialist did not return a response."
                    self.transcript.append(
                        session_id, "assistant", partial,
                        user_id=user_id,
                        request_id=request_id,
                    )
                    await self.manager.send_json(
                        session_id,
                        ev_chat_done(partial, request_id, aborted=False),
                    )
                    return

                prepass = SpecialistPrepassResult()
                try:
                    prepass = await self._run_specialist_prepass(
                        session_id=session_id,
                        user_id=user_id,
                        message=message,
                        specialist_name=decision.specialist,
                        request_id=request_id,
                    )
                except Exception as exc:
                    await self._emit_routing_event(
                        session_id,
                        status="fallback",
                        message=(
                            f"{decision.specialist} prepass failed; "
                            f"falling back to root_agent ({exc})."
                        ),
                        user_id=user_id,
                        agent_name="root_agent",
                    )
                    prepass = SpecialistPrepassResult()
                if prepass.infrastructure_blocked:
                    partial = self._format_specialist_runtime_failure(
                        decision.specialist,
                        prepass,
                    )
                    await self._emit_routing_event(
                        session_id,
                        status="blocked",
                        message=(
                            f"{decision.specialist} runtime unavailable; "
                            "not forwarding browser context to root_agent."
                        ),
                        user_id=user_id,
                        agent_name=decision.specialist,
                    )
                    self.transcript.append(
                        session_id,
                        "assistant",
                        partial,
                        user_id=user_id,
                        request_id=request_id,
                        metadata={"type": "specialist_runtime_error"},
                    )
                    await self.manager.send_json(
                        session_id,
                        ev_chat_done(partial, request_id, aborted=False),
                    )
                    return
                routed_message = self._format_root_routing_message(
                    message,
                    decision,
                    specialist_output=prepass.text,
                    specialist_evidence=prepass.evidence_blocks,
                )
                await self._emit_routing_event(
                    session_id,
                    status="forwarded",
                    message=(
                        f"Routing context from {decision.specialist} forwarded to root_agent."
                    ),
                    user_id=user_id,
                    agent_name="root_agent",
                )

            full_msg = await self._compose_grounded_agent_message(
                session_id,
                user_id,
                routed_message,
                research_message=message,
                agent_name=root_agent.name,
                request_id=request_id,
                emit_tool_events=True,
                allow_forced_research=not (
                    decision.target == "specialist"
                    and decision.specialist == "web_researcher"
                    and decision.preflight_specialist
                ),
            )
            content = types.Content(role="user", parts=[types.Part(text=full_msg)])

            async with asyncio.timeout(_AGENT_TIMEOUT):
                async for event in self.runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=content,
                ):
                    await self._emit_runner_tool_events(
                        session_id,
                        event,
                        fallback_request_id=request_id,
                    )
                    if event.is_final_response() and event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                partial += part.text

            if not partial.strip():
                partial = "MissionOS could not generate a response. Try again or make the request more specific."

            self.audit_logger.log_agent_message(
                agent_name="root_agent",
                message=partial,
                user_id=user_id,
                session_id=session_id,
            )
            # Persist to transcript
            self.transcript.append(
                session_id, "assistant", partial,
                user_id=user_id,
                request_id=request_id,
            )
            await self.manager.send_json(session_id, ev_chat_done(partial, request_id, aborted=False))

        except asyncio.CancelledError:
            # Persist partial + aborted flag to transcript
            self.transcript.append(
                session_id, "assistant", partial,
                user_id=user_id,
                request_id=request_id,
                aborted=True,
            )
            await self.manager.send_json(
                session_id,
                ev_chat_done(partial, request_id, aborted=True),
            )
            raise

        except TimeoutError:
            msg = f"Agent timed out after {_AGENT_TIMEOUT} seconds."
            self.audit_logger.log_error(error=msg, user_id=user_id, session_id=session_id,
                                        context={"reason": "timeout"})
            self.transcript.append(
                session_id, "assistant", msg,
                user_id=user_id,
                request_id=request_id,
                metadata={"error": "timeout"},
            )
            await self.manager.send_json(session_id, ev_chat_done(msg, request_id, aborted=False))

        except Exception as exc:
            self.audit_logger.log_error(error=str(exc), user_id=user_id, session_id=session_id,
                                        context={"message": message})
            error_msg = f"Error: {exc}"
            self.transcript.append(
                session_id, "assistant", error_msg,
                user_id=user_id,
                request_id=request_id,
                metadata={"error": str(exc)},
            )
            await self.manager.send_json(
                session_id,
                ev_chat_done(error_msg, request_id, aborted=False),
            )

        finally:
            self.manager.clear_task(session_id)

    async def _control_loop_task(
        self,
        session_id: str,
        user_id: str,
        goal: str,
        constraints: list[str],
        request_id: Optional[str] = None,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        initial_state: Optional[dict[str, Any]] = None,
        reset_if_terminal: bool = False,
    ) -> None:
        try:
            result = await self._run_control_loop_http(
                user_id=user_id,
                session_id=session_id,
                goal=goal,
                constraints=constraints,
                request_id=request_id,
                source="websocket",
                preserve_control_ui_tab=True,
                task_id=task_id,
                parent_task_id=parent_task_id,
                replay_of_task_id=replay_of_task_id,
                compare_to_task_id=compare_to_task_id,
                initial_state=initial_state,
                reset_if_terminal=reset_if_terminal,
            )
            self.transcript.append(
                session_id,
                "assistant",
                result.final_text,
                user_id=user_id,
                request_id=request_id,
                metadata={
                    "type": "control_loop",
                    "success": result.success,
                    "needs_human": bool(result.metadata.get("needs_human")),
                    "plan_id": result.plan_id,
                    "task_id": result.metadata.get("task_id"),
                },
            )
            if result.metadata.get("needs_human"):
                await self._emit_control_approval_request(
                    session_id,
                    result.metadata.get("approval_request"),
                )
            await self.manager.send_json(
                session_id,
                ev_chat_done(result.final_text, request_id, aborted=False),
            )
        except asyncio.CancelledError:
            await self.manager.send_json(
                session_id,
                ev_chat_done("", request_id, aborted=True),
            )
            raise
        except Exception as exc:
            error_msg = f"Control loop error: {exc}"
            self.transcript.append(
                session_id,
                "assistant",
                error_msg,
                user_id=user_id,
                request_id=request_id,
                metadata={"type": "control_loop", "error": str(exc)},
            )
            await self.manager.send_json(
                session_id,
                ev_chat_done(error_msg, request_id, aborted=False),
            )
        finally:
            self.manager.clear_task(session_id)

    def _merge_control_constraints(
        self,
        *,
        goal: str,
        constraints: list[str],
        preserve_control_ui_tab: bool,
    ) -> list[str]:
        effective_constraints = list(constraints)
        if prefers_isolated_browser_for_goal(goal):
            for item in _ISOLATED_BROWSER_TEXT_ENTRY_CONSTRAINTS:
                if item not in effective_constraints:
                    effective_constraints.append(item)
            return effective_constraints
        if not targets_user_browser(goal):
            return effective_constraints
        for item in _CURRENT_BROWSER_CONTROL_BASE_CONSTRAINTS:
            if item not in effective_constraints:
                effective_constraints.append(item)
        if preserve_control_ui_tab:
            if (
                _CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT
                in effective_constraints
            ):
                effective_constraints.remove(
                    _CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT
                )
            if (
                _CURRENT_BROWSER_PRESERVE_USER_TAB_CONSTRAINT
                not in effective_constraints
            ):
                effective_constraints.append(
                    _CURRENT_BROWSER_PRESERVE_USER_TAB_CONSTRAINT
                )
        elif (
            _CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT
            not in effective_constraints
        ):
            effective_constraints.append(
                _CURRENT_BROWSER_CONTROL_SAME_TAB_CONSTRAINT
            )
        return effective_constraints

    @staticmethod
    def _should_expand_control_loop_followup(message: str) -> bool:
        normalized = str(message or "").strip().lower()
        if not normalized or len(normalized) > 40:
            return False
        if any(marker in normalized for marker in _CONTROL_LOOP_FOLLOWUP_MARKERS):
            return True
        return any(keyword in normalized for keyword in SPREADSHEET_KEYWORDS)

    def _latest_completed_control_loop_resume_context(
        self,
        *,
        session_id: str,
    ) -> dict[str, Any] | None:
        payload = self.task_store.query(
            owner_session_id=session_id,
            kind="control_loop",
            status="completed",
            page=1,
            page_size=10,
        )
        tasks = payload.get("tasks")
        tasks = tasks if isinstance(tasks, list) else []
        for task in tasks:
            artifacts = task.get("artifacts")
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            resume_context = artifacts.get("resume_context")
            if isinstance(resume_context, dict) and str(resume_context.get("goal") or "").strip():
                return resume_context
        return None

    def _resolve_control_loop_goal(
        self,
        *,
        session_id: str,
        goal: str,
    ) -> str:
        normalized_goal = str(goal or "").strip()
        if not self._should_expand_control_loop_followup(normalized_goal):
            return normalized_goal
        resume_context = self._latest_completed_control_loop_resume_context(
            session_id=session_id,
        )
        if not isinstance(resume_context, dict):
            return normalized_goal
        prior_goal = str(resume_context.get("goal") or "").strip()
        if not prior_goal or prior_goal == normalized_goal:
            return normalized_goal
        return (
            f"{prior_goal}\n\n"
            f"Follow-up instruction: {normalized_goal}"
        )

    # HTTP agent execution (no abort support)
    async def _run_agent_http(self, user_id: str, session_id: str, message: str) -> dict:
        if is_direct_stock_price_query(message):
            await self._send_tool_event(
                session_id,
                ev_tool_start(
                    tool_name="stock_price",
                    agent_name=root_agent.name,
                    args={"query": message},
                ),
            )
            quote = await stock_price(message)
            await self._send_tool_event(
                session_id,
                ev_tool_result(
                    tool_name="stock_price",
                    agent_name=root_agent.name,
                    ok=bool(quote.get("ok")),
                    result=self._summarize_tool_result(quote),
                ),
            )
            if quote.get("ok"):
                text = (
                    f"Latest daily data for {quote.get('symbol')}:\n"
                    f"- Date: {quote.get('date')}\n"
                    f"- Open: {quote.get('open')}\n"
                    f"- High: {quote.get('high')}\n"
                    f"- Low: {quote.get('low')}\n"
                    f"- Close: {quote.get('close')}\n"
                    f"- Volume: {quote.get('volume')}"
                )
            else:
                text = quote.get("message", "Could not retrieve stock price data.")
            return {
                "type": "agent_message",
                "message": text,
                "ok": bool(quote.get("ok")),
            }

        effective_message = self._resolve_control_loop_goal(
            session_id=session_id,
            goal=message,
        )
        decision = await self._select_route_for_message(
            session_id=session_id,
            user_id=user_id,
            message=effective_message,
            source="http",
        )
        if decision.target == "control_loop":
            await self._emit_routing_event(
                session_id,
                status="selected",
                message=f"Router selected control loop ({decision.reason or 'multi-step task'}).",
                user_id=user_id,
                agent_name="root_workflow",
            )
            result = await self._run_control_loop_http(
                user_id=user_id,
                session_id=session_id,
                goal=effective_message,
                constraints=[],
                source="http",
                reset_if_terminal=True,
            )
            if result.metadata.get("needs_human"):
                await self._emit_control_approval_request(
                    session_id,
                    result.metadata.get("approval_request"),
                )
            return {
                "type": "control_loop",
                "message": result.final_text,
                "ok": result.success,
                "task_id": result.metadata.get("task_id"),
            }

        if decision.target == "dynamic_agent":
            await self._emit_routing_event(
                session_id,
                status="selected",
                message=(
                    f"Router selected dynamic_agent "
                    f"({decision.reason or 'dedicated task environment'})."
                ),
                user_id=user_id,
                agent_name="dynamic_agent",
            )
            spawn = await self._spawn_dynamic_route(
                session_id=session_id,
                user_id=user_id,
                message=message,
                decision=decision,
            )
            if spawn.get("status") == "accepted":
                return {
                    "type": "dynamic_agent",
                    "message": (
                        "Dynamic agent started.\n"
                        f"- run_id: {spawn.get('run_id')}\n"
                        f"- mode: {decision.dynamic_agent.mode or 'run'}"
                    ),
                    "ok": True,
                }
            return {
                "type": "error",
                "message": spawn.get("error", "Failed to start dynamic agent."),
                "ok": False,
            }

        routed_message = message
        if decision.target == "specialist" and decision.specialist:
            await self._emit_routing_event(
                session_id,
                status="selected",
                message=(
                    f"Router selected {decision.specialist} "
                    f"({decision.reason or 'specialized task'})."
                ),
                user_id=user_id,
                agent_name=decision.specialist,
            )
            if not decision.preflight_specialist:
                prepass = await self._run_specialist_prepass(
                    session_id=session_id,
                    user_id=user_id,
                    message=message,
                    specialist_name=decision.specialist,
                )
                if prepass.infrastructure_blocked:
                    response_text = self._format_specialist_runtime_failure(
                        decision.specialist,
                        prepass,
                    )
                    return {
                        "type": "error",
                        "message": response_text,
                        "ok": False,
                    }
                response_text = prepass.text
                if not response_text.strip():
                    response_text = "Specialist did not return a response."
                return {
                    "type": "specialist",
                    "message": response_text,
                    "ok": True,
                }

            prepass = SpecialistPrepassResult()
            try:
                prepass = await self._run_specialist_prepass(
                    session_id=session_id,
                    user_id=user_id,
                    message=message,
                    specialist_name=decision.specialist,
                )
            except Exception as exc:
                await self._emit_routing_event(
                    session_id,
                    status="fallback",
                    message=(
                        f"{decision.specialist} prepass failed; "
                        f"falling back to root_agent ({exc})."
                    ),
                    user_id=user_id,
                    agent_name="root_agent",
                )
                prepass = SpecialistPrepassResult()
            if prepass.infrastructure_blocked:
                await self._emit_routing_event(
                    session_id,
                    status="blocked",
                    message=(
                        f"{decision.specialist} runtime unavailable; "
                        "not forwarding browser context to root_agent."
                    ),
                    user_id=user_id,
                    agent_name=decision.specialist,
                )
                return {
                    "type": "error",
                    "message": self._format_specialist_runtime_failure(
                        decision.specialist,
                        prepass,
                    ),
                    "ok": False,
                }
            routed_message = self._format_root_routing_message(
                message,
                decision,
                specialist_output=prepass.text,
                specialist_evidence=prepass.evidence_blocks,
            )
            await self._emit_routing_event(
                session_id,
                status="forwarded",
                message=(
                    f"Routing context from {decision.specialist} forwarded to root_agent."
                ),
                user_id=user_id,
                agent_name="root_agent",
            )

        full_msg = await self._compose_grounded_agent_message(
            session_id,
            user_id,
            routed_message,
            research_message=message,
            agent_name=root_agent.name,
            emit_tool_events=False,
            allow_forced_research=not (
                decision.target == "specialist"
                and decision.specialist == "web_researcher"
                and decision.preflight_specialist
            ),
        )
        content = types.Content(role="user", parts=[types.Part(text=full_msg)])

        try:
            response_text = ""
            async with asyncio.timeout(_AGENT_TIMEOUT):
                async for event in self.runner.run_async(
                    user_id=user_id,
                    session_id=session_id,
                    new_message=content,
                ):
                    await self._emit_runner_tool_events(
                        session_id,
                        event,
                    )
                    if event.is_final_response() and event.content and event.content.parts:
                        for part in event.content.parts:
                            if part.text:
                                response_text += part.text

            if not response_text.strip():
                response_text = "MissionOS could not generate a response. Try again or make the request more specific."

            self.audit_logger.log_agent_message(
                agent_name="root_agent",
                message=response_text,
                user_id=user_id,
                session_id=session_id,
            )
            return {"type": "agent_message", "message": response_text, "ok": True}

        except TimeoutError:
            msg = f"Agent timed out after {_AGENT_TIMEOUT} seconds."
            self.audit_logger.log_error(
                error=msg,
                user_id=user_id,
                session_id=session_id,
                context={"message": message, "reason": "timeout"},
            )
            return {"type": "error", "message": msg, "ok": False}

        except Exception as exc:
            self.audit_logger.log_error(
                error=str(exc),
                user_id=user_id,
                session_id=session_id,
                context={"message": message},
            )
            return {"type": "error", "message": f"Error: {exc}", "ok": False}

    async def _run_control_loop_with_task(
        self,
        *,
        user_id: str,
        session_id: str,
        goal: str,
        constraints: list[str],
        request_id: Optional[str],
        source: str,
        preserve_control_ui_tab: bool,
        task_id: Optional[str] = None,
        owner_session_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        initial_state: Optional[dict[str, Any]] = None,
        reset_if_terminal: bool = False,
    ) -> tuple[ExecutionResult, str]:
        effective_constraints = self._merge_control_constraints(
            goal=goal,
            constraints=constraints,
            preserve_control_ui_tab=preserve_control_ui_tab,
        )
        artifacts, metadata = self._control_loop_seed_payload(
            goal=goal,
            constraints=effective_constraints,
            source=source,
            request_id=request_id,
            replay_of_task_id=replay_of_task_id,
            compare_to_task_id=compare_to_task_id,
            replay_from_step=(
                str(initial_state.get(StateKeys.REPLAY_FROM_STEP) or "").strip()
                if isinstance(initial_state, dict)
                else None
            ),
            replay_mode=(
                str((initial_state.get(StateKeys.REPLAY_CONTEXT) or {}).get("mode") or "").strip()
                if isinstance(initial_state, dict) and isinstance(initial_state.get(StateKeys.REPLAY_CONTEXT), dict)
                else None
            ),
        )
        if task_id:
            update_task_record(
                task_id,
                status="running",
                artifacts=artifacts,
                metadata=metadata,
                error=None,
            )
        else:
            task = self._create_control_loop_task_record(
                user_id=user_id,
                session_id=session_id,
                owner_session_id=owner_session_id,
                goal=goal,
                constraints=effective_constraints,
                request_id=request_id,
                source=source,
                parent_task_id=parent_task_id,
                replay_of_task_id=replay_of_task_id,
                compare_to_task_id=compare_to_task_id,
            )
            task_id = str(task["task_id"])

        current_browser_error = await self._current_browser_runtime_error(goal)
        if current_browser_error:
            update_task_record(
                task_id,
                status="failed",
                artifacts={
                    "result": {
                        "success": False,
                        "error": "desktop_bridge_unavailable",
                        "final_text": current_browser_error,
                    }
                },
                error="desktop_bridge_unavailable",
            )
            result = ExecutionResult(
                request_id=f"http_{uuid.uuid4().hex[:12]}",
                session_id=session_id,
                user_id=user_id,
                final_text=current_browser_error,
                success=False,
                metadata={"error": "desktop_bridge_unavailable", "task_id": task_id},
            )
            return result, task_id

        try:
            result = await self.control_loop.run(
                goal=goal,
                user_id=user_id,
                constraints=effective_constraints,
                session_id=session_id,
                initial_state=initial_state,
                reset_if_terminal=reset_if_terminal,
            )
        except Exception as exc:
            failure_type = "policy_blocked" if isinstance(exc, PermissionError) else "unknown"
            result = ExecutionResult(
                request_id=f"http_{uuid.uuid4().hex[:12]}",
                session_id=session_id,
                user_id=user_id,
                final_text=(
                    f"Control loop blocked by policy: {exc}"
                    if isinstance(exc, PermissionError)
                    else f"Control loop failed before producing a result: {exc}"
                ),
                success=False,
                metadata={
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                    "normalized_failure_type": failure_type,
                    "task_id": task_id,
                },
            )
        result.metadata["task_id"] = task_id
        needs_human = bool(result.metadata.get("needs_human"))
        approval_expired = (
            not result.success
            and not needs_human
            and any(
                reason in (result.final_text or "")
                for reason in APPROVAL_EXPIRY_REASONS
            )
        )
        error_text = None
        if not result.success and not needs_human:
            error_text = "approval_expired" if approval_expired else (result.final_text or "control loop failed")
        failure_classification = classify_control_loop_failure(
            success=result.success,
            needs_human=needs_human,
            final_text=result.final_text,
            verification_status=result.metadata.get("verification_status"),
            verification_report=(
                result.metadata.get("verification_report")
                if isinstance(result.metadata.get("verification_report"), dict)
                else None
            ),
            error=error_text,
            existing_failure_type=result.metadata.get("normalized_failure_type"),
        )
        result.metadata.update(failure_classification)
        update_task_record(
            task_id,
            status="pending" if needs_human else ("completed" if result.success else "failed"),
            artifacts={
                "result": {
                    "success": result.success,
                    "final_text": result.final_text,
                    "plan_id": result.plan_id,
                    "verification_report_id": result.verification_report_id,
                    "verification_status": result.metadata.get("verification_status"),
                    "verification_report": result.metadata.get("verification_report"),
                    "verification_inputs": result.metadata.get("verification_inputs"),
                    "artifact_refs": result.metadata.get("artifact_refs"),
                    "approved_plan": result.metadata.get("approved_plan"),
                    "step_trace": result.metadata.get("step_trace"),
                    "tail_replay_from_step_id": result.metadata.get("tail_replay_from_step_id"),
                    "repair_count": result.repair_count,
                    "promoted_memory_ids": result.promoted_memory_ids,
                    "approval_request": result.metadata.get("approval_request"),
                    "preliminary_failure_type": failure_classification["preliminary_failure_type"],
                    "normalized_failure_type": failure_classification["normalized_failure_type"],
                    "classified_by": failure_classification["classified_by"],
                    "operator_override": failure_classification["operator_override"],
                    **({"approval_expired": True} if approval_expired else {}),
                },
                "resume_context": {
                    "goal": goal,
                    "constraints": effective_constraints,
                    "plan_id": result.plan_id,
                    "approved_plan": result.metadata.get("approved_plan"),
                    "approval_request": result.metadata.get("approval_request"),
                },
            },
            metadata={
                "source": source,
                "request_id": request_id,
                "needs_human": needs_human,
                "normalized_failure_type": failure_classification["normalized_failure_type"],
                "classified_by": failure_classification["classified_by"],
                **(
                    {
                        "replay_from_step": str(initial_state.get(StateKeys.REPLAY_FROM_STEP) or "").strip(),
                    }
                    if isinstance(initial_state, dict) and initial_state.get(StateKeys.REPLAY_FROM_STEP)
                    else {}
                ),
                **({"approval_expired": True} if approval_expired else {}),
            },
            error=error_text,
        )
        persist_control_loop_step_events(task_id=task_id, result=result)
        return result, task_id

    async def _run_control_loop_http(
        self,
        *,
        user_id: str,
        session_id: str,
        goal: str,
        constraints: list[str],
        request_id: Optional[str] = None,
        source: str = "http",
        preserve_control_ui_tab: bool = False,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        replay_of_task_id: Optional[str] = None,
        compare_to_task_id: Optional[str] = None,
        initial_state: Optional[dict[str, Any]] = None,
        reset_if_terminal: bool = False,
    ):
        result, _task_id = await self._run_control_loop_with_task(
            user_id=user_id,
            session_id=session_id,
            goal=goal,
            constraints=constraints,
            request_id=request_id,
            source=source,
            preserve_control_ui_tab=preserve_control_ui_tab,
            task_id=task_id,
            parent_task_id=parent_task_id,
            replay_of_task_id=replay_of_task_id,
            compare_to_task_id=compare_to_task_id,
            initial_state=initial_state,
            reset_if_terminal=reset_if_terminal,
        )
        return result

    async def _emit_control_approval_request(
        self,
        session_id: str,
        approval_request: dict[str, Any] | None,
    ) -> None:
        if not approval_request:
            return
        await self.manager.send_or_queue_json(
            session_id,
            ev_control_approval_request(
                request_id=approval_request.get("request_id", ""),
                plan_id=approval_request.get("plan_id", ""),
                goal=approval_request.get("goal", ""),
                risk_level=approval_request.get("risk_level", ""),
                required_capabilities=approval_request.get(
                    "required_capabilities", []
                ),
                plan=approval_request.get("plan", {}),
                reason=approval_request.get("reason", ""),
            ),
        )

    async def _resolve_tool_approval_request(
        self,
        *,
        request_id: str,
        approved: bool,
        reason: str,
        session_id: str,
        user_id: str,
        source: str = "unknown",
        scope: Any = None,
        tool_pattern: Any = None,
        path_scope: Any = None,
        expires_at: Any = None,
        propagate_to_subagents: Any = None,
    ) -> dict[str, Any]:
        before = self.tool_policy.get_approval(request_id)
        requested_scope = scope if isinstance(scope, str) else None
        requested_tool_pattern = tool_pattern if isinstance(tool_pattern, str) else None
        requested_path_scope = path_scope if isinstance(path_scope, str) else None
        requested_propagate = (
            bool(propagate_to_subagents)
            if propagate_to_subagents is not None
            else None
        )
        result = self.tool_policy.resolve_approval(
            request_id,
            approved,
            reason,
            scope=requested_scope,
            tool_pattern=requested_tool_pattern,
            path_scope=requested_path_scope,
            expires_at=expires_at if isinstance(expires_at, (int, float)) else None,
            propagate_to_subagents=requested_propagate,
            history_metadata={
                "actor_user_id": user_id,
                "source": source,
                "scope_before": before.get("scope") if isinstance(before, dict) else None,
                "tool_pattern_before": before.get("tool_pattern") if isinstance(before, dict) else None,
                "path_scope_before": before.get("path_scope") if isinstance(before, dict) else None,
                "propagate_to_subagents_before": (
                    before.get("propagate_to_subagents") if isinstance(before, dict) else None
                ),
            },
        )
        control_loop_resolved = False
        pending_control_request = None
        if result is None:
            pending_control_request = await self.control_loop.get_pending_approval(
                user_id=user_id,
                session_id=session_id,
            )
            pending_control_task_id = self._find_control_loop_task_for_approval(
                session_id=session_id,
                request_id=request_id,
            )
            control_loop_resolved = await self.control_loop.resolve_human_approval(
                user_id=user_id,
                session_id=session_id,
                approved=approved,
                request_id=request_id,
            )
            if approved and control_loop_resolved and pending_control_request:
                resume_goal = (
                    await self.control_loop.get_task_goal(
                        user_id=user_id,
                        session_id=session_id,
                    )
                    or pending_control_request.get("goal", "")
                )
                await self._start_control_loop_run(
                    session_id=session_id,
                    user_id=user_id,
                    goal=resume_goal,
                    constraints=normalize_constraints(
                        (pending_control_request.get("plan") or {}).get("constraints")
                    ),
                    task_id=pending_control_task_id,
                )

        target_session_id = result.session_id if result else session_id
        status = "resolved" if result or control_loop_resolved else "not_found"
        await self._emit_session_event(
            target_session_id,
            source="tools.approval",
            status=status,
            message=f"Approval {request_id}: {'approved' if approved else 'denied'}",
            user_id=user_id,
        )
        response: dict[str, Any] = {
            "resolved": bool(result or control_loop_resolved),
            "request_id": request_id,
            "approved": approved,
            "status": status,
            "session_id": target_session_id,
        }
        audit_metadata: dict[str, Any] = {
            "request_id": request_id,
            "approved": approved,
            "source": source,
            "actor_user_id": user_id,
            "target_session_id": target_session_id,
        }
        if result is not None:
            response["approval"] = result.to_dict()
            after = result.to_dict()
            audit_metadata.update(
                {
                    "resolved_kind": "tool_approval",
                    "tool_name": after.get("tool_name"),
                    "agent_name": after.get("agent_name"),
                    "source_request_id": after.get("source_request_id"),
                    "state_before": before.get("state") if isinstance(before, dict) else None,
                    "state_after": after.get("state"),
                    "scope_before": before.get("scope") if isinstance(before, dict) else None,
                    "scope_after": after.get("scope"),
                    "tool_pattern_before": before.get("tool_pattern") if isinstance(before, dict) else None,
                    "tool_pattern_after": after.get("tool_pattern"),
                    "path_scope_before": before.get("path_scope") if isinstance(before, dict) else None,
                    "path_scope_after": after.get("path_scope"),
                    "propagate_to_subagents_before": (
                        before.get("propagate_to_subagents") if isinstance(before, dict) else None
                    ),
                    "propagate_to_subagents_after": after.get("propagate_to_subagents"),
                    "resolve_reason": reason,
                }
            )
        elif control_loop_resolved:
            response["control_loop"] = {"request_id": request_id, "approved": approved}
            audit_metadata.update(
                {
                    "resolved_kind": "control_loop",
                    "resolve_reason": reason,
                }
            )
        else:
            response["error"] = f"approval not found: {request_id}"
        self.audit_logger.log(
            event_type=AuditEventType.TOOL_APPROVAL,
            user_id=user_id or None,
            session_id=target_session_id or None,
            action="resolve",
            resource=request_id,
            result=status,
            metadata=audit_metadata,
        )
        return response

    async def _desktop_emergency_stop(
        self,
        *,
        session_id: str,
        user_id: str,
        reason: str,
    ) -> bool:
        if not getattr(self.settings, "desktop_bridge_enabled", False):
            return False
        try:
            from src.bridges.desktop_bridge_client import get_desktop_client
            from src.desktop import DesktopEmergencyStopRequest

            client = get_desktop_client()
            result = await client.emergency_stop(
                DesktopEmergencyStopRequest(
                    request_id=f"gateway-stop-{uuid.uuid4().hex[:12]}",
                    session_id=session_id,
                    user_id=user_id,
                    agent_name="gateway",
                    reason=reason,
                )
            )
            return bool(result.ok)
        except Exception as exc:
            self.audit_logger.log_error(
                error=str(exc),
                user_id=user_id,
                session_id=session_id,
                context={"action": "desktop_emergency_stop"},
            )
            return False

    async def _desktop_clear_stop(
        self,
        *,
        session_id: str,
        user_id: str,
    ) -> bool:
        if not getattr(self.settings, "desktop_bridge_enabled", False):
            return False
        try:
            from src.bridges.desktop_bridge_client import get_desktop_client
            from src.desktop import DesktopClearStopRequest

            client = get_desktop_client()
            result = await client.clear_stop(
                DesktopClearStopRequest(
                    request_id=f"gateway-clear-stop-{uuid.uuid4().hex[:12]}",
                    session_id=session_id,
                    user_id=user_id,
                    agent_name="gateway",
                )
            )
            return bool(result.ok)
        except Exception as exc:
            self.audit_logger.log_error(
                error=str(exc),
                user_id=user_id,
                session_id=session_id,
                context={"action": "desktop_clear_stop"},
            )
            return False

    # ------------------------------------------------------------------
    # session / transcript helpers
    # ------------------------------------------------------------------

    async def _get_or_create_gateway_session(
        self,
        *,
        user_id: str,
        session_id: Optional[str] = None,
    ):
        session = None
        if session_id:
            session = await self.session_service.get_session(
                app_name="boiled-claw",
                user_id=user_id,
                session_id=session_id,
            )
            if session is None and self.transcript.has_session(session_id, user_id):
                session = await self._hydrate_session_from_transcript(user_id, session_id)

        if session is None:
            session = await self.session_service.create_session(
                app_name="boiled-claw",
                user_id=user_id,
                session_id=session_id,
            )

        self.transcript.ensure_session(session.id, user_id)
        return session

    async def _hydrate_session_from_transcript(self, user_id: str, session_id: str):
        session = await self.session_service.create_session(
            app_name="boiled-claw",
            user_id=user_id,
            session_id=session_id,
        )
        for entry in self.transcript.get_history(session_id, limit=500):
            if entry.role not in {"user", "assistant"}:
                continue
            if not entry.content.strip():
                continue
            content_role = "user" if entry.role == "user" else "model"
            author = "user" if entry.role == "user" else root_agent.name
            event = Event(
                invocation_id=f"hydrated:{session_id}",
                author=author,
                content=types.Content(
                    role=content_role,
                    parts=[types.Part(text=entry.content)],
                ),
                timestamp=entry.created_at,
            )
            await self.session_service.append_event(session, event)
        return session

    def _compose_agent_message(self, session_id: str, message: str) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        history = self.transcript.get_history(session_id, limit=200)
        inject_lines = []
        for entry in history:
            if entry.role != "inject":
                continue
            inject_role = entry.metadata.get("role", "system")
            inject_lines.append(f"[inject:{inject_role}] {entry.content}")

        preface = [f"[System information: current local datetime is {now}]"]
        if inject_lines:
            preface.append("[Gateway inject context]")
            preface.extend(inject_lines[-10:])
        preface.append("")
        preface.append(message)
        return "\n".join(preface)

    def _resolve_cron_delivery_target(self, payload: Dict[str, Any]) -> str:
        delivery_target = str(payload.get("delivery_target") or "isolated").strip()
        bound_session_id = str(payload.get("session_id") or "").strip()
        system_event = payload.get("system_event") or None
        if delivery_target == "main":
            if bound_session_id:
                return f"session:{bound_session_id}"
            if system_event in {"connect", "disconnect"}:
                return "main"
            raise ValueError(
                "delivery_target='main' requires either a session_id binding "
                "or a connect/disconnect system_event trigger"
            )
        return delivery_target or "isolated"

    async def _emit_session_event(
        self,
        session_id: str,
        *,
        source: str,
        status: str,
        message: str,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> None:
        event = ev_system_event(
            source=source,
            status=status,
            message=message,
            run_id=run_id,
            task_id=task_id,
            agent_name=agent_name,
        )
        session = self.transcript.get_session(session_id)
        resolved_user = user_id or (session.user_id if session else None)
        if (
            session is not None
            and resolved_user
            and source != "tools.approval"
        ):
            self.transcript.append(
                session_id,
                "system",
                message,
                user_id=resolved_user,
                metadata={
                    "source": source,
                    "status": status,
                    "run_id": run_id or "",
                    "task_id": task_id or "",
                    "agent_name": agent_name or "",
                },
            )
        elif session_id not in self.manager.active_connections:
            return
        await self.manager.send_or_queue_json(session_id, event)

    # ------------------------------------------------------------------
    # heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            # Sweep approval expiry so expiring/expired notifications fire
            # even when no new approval request arrives.
            self.tool_policy.cleanup_expired()
            if self.manager.active_connections:
                tick = ev_health_tick(len(self.manager.active_connections))
                await self.manager.broadcast_json(tick)
                await self.manager.flush_all_pending()

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def run(self, host: Optional[str] = None, port: Optional[int] = None):
        import uvicorn
        uvicorn.run(
            self.app,
            host=host or self.settings.gateway_host,
            port=port or self.settings.gateway_port,
        )
def create_gateway() -> GatewayServer:
    return GatewayServer()
