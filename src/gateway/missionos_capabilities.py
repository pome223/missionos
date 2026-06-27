"""MissionOS internal capability registry.

The Chief Agent is the operator-facing ingress, but Gateway remains the
authority boundary.  This registry gives the Chief a typed description of the
internal capabilities it may propose while keeping invocation, approval,
execution, and artifact persistence in Gateway.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import uuid
from typing import Any, Mapping


MISSIONOS_CAPABILITY_REGISTRY_SCHEMA_VERSION = (
    "missionos_internal_capability_registry.v1"
)
MISSIONOS_CAPABILITY_INVOCATION_SCHEMA_VERSION = (
    "missionos_internal_capability_invocation.v1"
)
MISSIONOS_APPROVAL_REQUEST_TOOL_SCHEMA_VERSION = (
    "missionos_approval_request_tool.v1"
)

MISSIONOS_OPERATOR_FACING_ROUTE = "/missionos/autonomy-conversation/run"


MISSIONOS_INTERNAL_CAPABILITIES: dict[str, dict[str, Any]] = {
    "form2a_response_selection": {
        "capability_id": "form2a_response_selection",
        "label": "Form 2a response selection",
        "route": "/missionos/form2a-response-selection/run",
        "summary_route": "/missionos/form2a-response-selection",
        "kind": "internal_capability",
        "owner": "Gateway",
        "chief_may": [
            "propose using source-bound Form 1 evidence",
            "pass operator_instruction as proposal context",
        ],
        "gateway_owns": [
            "artifact hash checks",
            "response allowlist",
            "operator approval token issuance",
            "dispatch suppression before approval",
        ],
        "state_change": "writes_response_selection_and_approval_request_artifacts",
        "operator_facing": False,
    },
    "form2a_operator_review": {
        "capability_id": "form2a_operator_review",
        "label": "Form 2a operator approval review",
        "route": "/missionos/form2a-operator-review/approve",
        "summary_route": "/missionos/form2a-operator-review",
        "kind": "approval_tool",
        "owner": "Gateway",
        "chief_may": [
            "ask the operator for approval",
            "summarize the approval scope",
        ],
        "gateway_owns": [
            "approval token binding",
            "selection artifact hash check",
            "human review artifact persistence",
        ],
        "state_change": "writes_human_operator_review_artifact",
        "operator_facing": False,
    },
    "llm_repair_planning": {
        "capability_id": "llm_repair_planning",
        "label": "LLM repair planning",
        "route": "/missionos/llm-repair-planner/run",
        "summary_route": "/missionos/llm-repair-planner",
        "kind": "internal_capability",
        "owner": "Gateway",
        "coordinated_by": "missionos_repair_planner_agent",
        "phase": "post_block_or_next_run_planning",
        "chief_may": [
            "request a bounded repair proposal from blocked evidence",
            "explain repair uncertainty",
        ],
        "gateway_owns": [
            "source-bound evidence persistence",
            "repair proposal guardrails",
            "approval and execution separation",
        ],
        "state_change": "writes_repair_input_or_repair_proposal_artifacts",
        "operator_facing": False,
    },
    "runtime_recovery": {
        "capability_id": "runtime_recovery",
        "label": "Runtime recovery assessment",
        "route": "/missionos/runtime-recovery-agent/run",
        "summary_route": "",
        "kind": "internal_capability",
        "owner": "Gateway",
        "chief_may": [
            "request telemetry-driven recovery judgment",
            "surface operator review when needed",
        ],
        "gateway_owns": [
            "telemetry risk checks",
            "preauthorized action checks",
            "dispatch suppression before approval or preauthorization",
        ],
        "state_change": "returns_recovery_assessment_only",
        "operator_facing": False,
    },
    "execution_handoff": {
        "capability_id": "execution_handoff",
        "label": "Approved execution handoff",
        "route": "/missionos/form2a-action-consumption/run",
        "summary_route": "/missionos/form2a-action-consumption",
        "kind": "execution_boundary",
        "owner": "Gateway",
        "chief_may": [
            "explain the next execution handoff after approval",
        ],
        "gateway_owns": [
            "approval token consumption",
            "dispatch receipt",
            "runtime invocation evidence",
            "verifier evidence",
        ],
        "state_change": "may_execute_only_after_approval_and_gateway_checks",
        "operator_facing": False,
    },
}


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(dict(payload), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def build_missionos_capability_registry_summary() -> dict[str, Any]:
    return {
        "schema_version": MISSIONOS_CAPABILITY_REGISTRY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "operator_facing_route": MISSIONOS_OPERATOR_FACING_ROUTE,
        "capability_count": len(MISSIONOS_INTERNAL_CAPABILITIES),
        "capabilities": {
            key: dict(value)
            for key, value in sorted(MISSIONOS_INTERNAL_CAPABILITIES.items())
        },
        "authority_boundary": {
            "chief_invokes_tools_directly": False,
            "gateway_executes_internal_capabilities": True,
            "llm_judgment_in_gate": False,
            "approval_required_for_state_changing_actions": True,
        },
    }


def capability_descriptor_for_prompt(capability_id: str) -> dict[str, Any]:
    capability = dict(MISSIONOS_INTERNAL_CAPABILITIES[capability_id])
    return {
        "capability_id": capability["capability_id"],
        "label": capability["label"],
        "kind": capability["kind"],
        "route": capability["route"],
        "chief_may": list(capability.get("chief_may") or []),
        "gateway_owns": list(capability.get("gateway_owns") or []),
        "state_change": capability["state_change"],
    }


def all_capability_descriptors_for_prompt() -> list[dict[str, Any]]:
    return [
        capability_descriptor_for_prompt(capability_id)
        for capability_id in sorted(MISSIONOS_INTERNAL_CAPABILITIES)
    ]


def capability_invocation_context(
    capability_id: str,
    *,
    requested_by: str = "direct_gateway_route",
    operator_facing_route: str = MISSIONOS_OPERATOR_FACING_ROUTE,
    chief_agent_invocation_ref: str = "",
    specialist_agent_invocation_ref: str = "",
    safety_critic_ref: str = "",
    source_route: str = "",
    request_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    capability = MISSIONOS_INTERNAL_CAPABILITIES[capability_id]
    generated_at = datetime.now(timezone.utc).isoformat()
    request_hash = _sha256_json(request_payload or {})
    invocation_id = (
        f"missionos_internal_capability_invocation_{uuid.uuid4().hex[:12]}"
    )
    return {
        "schema_version": MISSIONOS_CAPABILITY_INVOCATION_SCHEMA_VERSION,
        "capability_invocation_id": invocation_id,
        "capability_invocation_ref": (
            f"missionos_internal_capability_invocation:{invocation_id}"
        ),
        "capability_id": capability_id,
        "capability_label": capability["label"],
        "capability_kind": capability["kind"],
        "capability_route": capability["route"],
        "operator_facing_route": operator_facing_route,
        "requested_by": requested_by,
        "source_route": source_route,
        "generated_at": generated_at,
        "request_payload_sha256": request_hash,
        "chief_agent_invocation_ref": chief_agent_invocation_ref,
        "specialist_agent_invocation_ref": specialist_agent_invocation_ref,
        "safety_critic_ref": safety_critic_ref,
        "gateway_executes_internal_capability": True,
        "llm_judgment_in_gate": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }


def approval_request_tool_record(
    *,
    approval_scope: str,
    approval_payload: Mapping[str, Any],
    capability_context: Mapping[str, Any],
    approval_ref: str,
    approval_artifact_path: str,
    expires_at: str,
) -> dict[str, Any]:
    request_id = f"missionos_approval_request_{uuid.uuid4().hex[:12]}"
    payload_hash = _sha256_json(approval_payload)
    return {
        "schema_version": MISSIONOS_APPROVAL_REQUEST_TOOL_SCHEMA_VERSION,
        "approval_request_id": request_id,
        "approval_request_ref": f"missionos_approval_request_tool:{request_id}",
        "approval_request_status": "pending_operator_decision",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "approval_scope": approval_scope,
        "approval_payload_sha256": payload_hash,
        "approval_ref": approval_ref,
        "approval_artifact_path": approval_artifact_path,
        "expires_at": expires_at,
        "capability_invocation_ref": capability_context.get(
            "capability_invocation_ref", ""
        ),
        "capability_id": capability_context.get("capability_id", ""),
        "operator_facing_route": capability_context.get(
            "operator_facing_route", MISSIONOS_OPERATOR_FACING_ROUTE
        ),
        "requested_by": capability_context.get("requested_by", "direct_gateway_route"),
        "chief_agent_invocation_ref": capability_context.get(
            "chief_agent_invocation_ref", ""
        ),
        "specialist_agent_invocation_ref": capability_context.get(
            "specialist_agent_invocation_ref", ""
        ),
        "safety_critic_ref": capability_context.get("safety_critic_ref", ""),
        "tool_confirmation_required": True,
        "operator_confirmed": False,
        "confirmed_at": "",
        "confirmed_by": "",
        "confirmation_consumed": False,
        "llm_judgment_in_gate": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
    }


__all__ = [
    "MISSIONOS_APPROVAL_REQUEST_TOOL_SCHEMA_VERSION",
    "MISSIONOS_CAPABILITY_INVOCATION_SCHEMA_VERSION",
    "MISSIONOS_CAPABILITY_REGISTRY_SCHEMA_VERSION",
    "MISSIONOS_INTERNAL_CAPABILITIES",
    "MISSIONOS_OPERATOR_FACING_ROUTE",
    "all_capability_descriptors_for_prompt",
    "approval_request_tool_record",
    "build_missionos_capability_registry_summary",
    "capability_descriptor_for_prompt",
    "capability_invocation_context",
]
