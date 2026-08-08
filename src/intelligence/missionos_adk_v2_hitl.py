"""ADK v2 HITL transport bound to canonical MissionOS approval artifacts.

RequestInput pauses and resumes orchestration only. The human response must
carry a canonical ``approval_ref`` and a MissionOS validator must reload that
artifact and verify its binding. A response such as ``yes`` creates no
approval, dispatch authority, execution fact, effect, or progress.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import json
from typing import Any
import uuid


MISSIONOS_ADK_V2_HITL_ENV = "MISSIONOS_ADK_V2_HITL_ENABLED"
MISSIONOS_ADK_V2_HITL_APP_NAME = "missionos_adk_v2_canonical_approval_hitl"
MISSIONOS_ADK_V2_HITL_WORKFLOW_NAME = "missionos_canonical_approval_hitl_v2"
MISSIONOS_ADK_V2_HITL_RESULT_SCHEMA_VERSION = "missionos_adk_v2_hitl_result.v1"
MISSIONOS_ADK_V2_HITL_STATE_KEY = "missionos_adk_v2_hitl_expected_binding"

CanonicalApprovalValidator = Callable[
    [str, Mapping[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


def _authority_floor() -> dict[str, bool]:
    return {
        "approval_created": False,
        "dispatch_authority_created": False,
        "executor_invoked": False,
        "physical_execution_invoked": False,
        "outcome_observed": False,
        "progress_counted": False,
    }


def _content_text(value: Any) -> str:
    parts = getattr(value, "parts", None) or []
    return "".join(
        str(getattr(part, "text", "") or "")
        for part in parts
        if getattr(part, "text", None)
    )


def _session_backend_name(session_service: Any) -> str:
    name = type(session_service).__name__
    return "redis" if name == "RedisSessionService" else "memory"


def _blocked_result(
    reasons: list[str],
    *,
    approval_ref: str = "",
    human_input_received: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": MISSIONOS_ADK_V2_HITL_RESULT_SCHEMA_VERSION,
        "hitl_status": "approval_blocked",
        "blocking_reasons": reasons,
        "approval_ref": approval_ref,
        "canonical_approval_validated": False,
        "human_input_received": human_input_received,
        "human_input_created_authority": False,
        "workflow_name": MISSIONOS_ADK_V2_HITL_WORKFLOW_NAME,
        **_authority_floor(),
    }


async def _resolve_validation(
    validator: CanonicalApprovalValidator,
    approval_ref: str,
    expected_binding: Mapping[str, Any],
) -> dict[str, Any]:
    validation = validator(approval_ref, expected_binding)
    if hasattr(validation, "__await__"):
        validation = await validation  # type: ignore[misc]
    return dict(validation) if isinstance(validation, Mapping) else {}


def build_missionos_canonical_approval_hitl_workflow(
    *,
    approval_validator: CanonicalApprovalValidator,
) -> Any:
    """Build a fresh workflow whose validator reloads MissionOS authority."""

    from google.adk import Event, Workflow
    from google.adk.events import RequestInput
    from google.adk.workflow import node

    @node(name="bind_canonical_approval_request", rerun_on_resume=False)
    def bind_canonical_approval_request(node_input: Any) -> Event:
        try:
            payload = json.loads(_content_text(node_input))
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        required_fields = (
            "interrupt_id",
            "operator_session_id",
            "approval_ref",
            "mission_response_candidate_ref",
            "proposal_sha256",
            "bounded_action_ref",
            "dispatch_ref",
        )
        missing = [field for field in required_fields if not str(payload.get(field) or "").strip()]
        if missing:
            payload = {
                "binding_status": "blocked",
                "blocking_reasons": [f"canonical_approval_binding_missing:{field}" for field in missing],
            }
        else:
            payload["binding_status"] = "ready"
            payload["blocking_reasons"] = []
        return Event(
            output=payload,
            state={MISSIONOS_ADK_V2_HITL_STATE_KEY: payload},
        )

    @node(name="await_canonical_missionos_approval", rerun_on_resume=False)
    async def await_canonical_missionos_approval(node_input: Mapping[str, Any]):
        if node_input.get("binding_status") != "ready":
            return
        yield RequestInput(
            interrupt_id=str(node_input["interrupt_id"]),
            message=(
                "Resolve the canonical MissionOS approval artifact, then submit "
                "its exact approval_ref. A yes/no response is not approval."
            ),
            payload={
                "approval_ref": node_input["approval_ref"],
                "mission_response_candidate_ref": node_input[
                    "mission_response_candidate_ref"
                ],
                "proposal_sha256": node_input["proposal_sha256"],
                "bounded_action_ref": node_input["bounded_action_ref"],
                "dispatch_ref": node_input["dispatch_ref"],
                "expires_at": node_input.get("expires_at"),
                **_authority_floor(),
            },
            response_schema={
                "type": "object",
                "properties": {"approval_ref": {"type": "string"}},
                "required": ["approval_ref"],
                "additionalProperties": False,
            },
        )

    @node(name="validate_canonical_missionos_approval", rerun_on_resume=True)
    async def validate_canonical_missionos_approval(
        ctx: Any,
        node_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        expected_value = ctx.state.get(MISSIONOS_ADK_V2_HITL_STATE_KEY)
        expected = expected_value if isinstance(expected_value, Mapping) else {}
        approval_ref = str(node_input.get("approval_ref") or "").strip()
        if not expected:
            return _blocked_result(
                ["canonical_approval_expected_binding_missing"],
                approval_ref=approval_ref,
                human_input_received=True,
            )
        if not approval_ref:
            return _blocked_result(
                ["adk_request_input_approval_ref_required"],
                human_input_received=True,
            )
        if approval_ref != str(expected.get("approval_ref") or ""):
            return _blocked_result(
                ["adk_request_input_approval_ref_mismatch"],
                approval_ref=approval_ref,
                human_input_received=True,
            )
        try:
            validation = await _resolve_validation(
                approval_validator,
                approval_ref,
                expected,
            )
        except Exception as exc:  # pragma: no cover - backend failures vary.
            return _blocked_result(
                [f"canonical_approval_validation_failed:{type(exc).__name__}"],
                approval_ref=approval_ref,
                human_input_received=True,
            )
        validated = (
            validation.get("validation_status") == "approved"
            and validation.get("canonical_approval_validated") is True
        )
        if not validated:
            reasons = list(validation.get("blocking_reasons") or [])
            return _blocked_result(
                reasons or ["canonical_approval_not_validated"],
                approval_ref=approval_ref,
                human_input_received=True,
            )
        return {
            "schema_version": MISSIONOS_ADK_V2_HITL_RESULT_SCHEMA_VERSION,
            "hitl_status": "canonical_approval_validated",
            "blocking_reasons": [],
            "approval_ref": approval_ref,
            "canonical_approval_validated": True,
            "canonical_approval_validation": validation,
            "mission_response_candidate_ref": expected.get(
                "mission_response_candidate_ref"
            ),
            "proposal_sha256": expected.get("proposal_sha256"),
            "bounded_action_ref": expected.get("bounded_action_ref"),
            "dispatch_ref": expected.get("dispatch_ref"),
            "human_input_received": True,
            "human_input_created_authority": False,
            "workflow_name": MISSIONOS_ADK_V2_HITL_WORKFLOW_NAME,
            **_authority_floor(),
        }

    @node(name="finalize_canonical_approval_resume", rerun_on_resume=True)
    def finalize_canonical_approval_resume(node_input: Mapping[str, Any]) -> dict[str, Any]:
        if node_input.get("schema_version") == MISSIONOS_ADK_V2_HITL_RESULT_SCHEMA_VERSION:
            return dict(node_input)
        return _blocked_result(
            list(node_input.get("blocking_reasons") or ["canonical_approval_resume_invalid"]),
        )

    return Workflow(
        name=MISSIONOS_ADK_V2_HITL_WORKFLOW_NAME,
        description=(
            "Pause for a canonical MissionOS approval reference and revalidate "
            "the artifact without creating dispatch authority."
        ),
        rerun_on_resume=True,
        edges=[
            (
                "START",
                bind_canonical_approval_request,
                await_canonical_missionos_approval,
                validate_canonical_missionos_approval,
                finalize_canonical_approval_resume,
            )
        ],
    )


def _pending_request_input(events: list[Any]) -> dict[str, Any]:
    requested: list[dict[str, Any]] = []
    resolved: set[str] = set()
    for event in events:
        content = getattr(event, "content", None)
        for part in getattr(content, "parts", None) or []:
            function_response = getattr(part, "function_response", None)
            if function_response and getattr(function_response, "id", None):
                resolved.add(str(function_response.id))
            function_call = getattr(part, "function_call", None)
            if (
                function_call
                and getattr(function_call, "name", "") == "adk_request_input"
                and getattr(function_call, "id", None)
            ):
                requested.append(
                    {
                        "interrupt_id": str(function_call.id),
                        "args": dict(function_call.args or {}),
                    }
                )
    for request in reversed(requested):
        if request["interrupt_id"] not in resolved:
            return request
    return {}


async def start_missionos_canonical_approval_hitl(
    *,
    session_service: Any,
    operator_session_id: str,
    approval_binding: Mapping[str, Any],
    approval_validator: CanonicalApprovalValidator,
    adk_session_id: str = "",
) -> dict[str, Any]:
    """Start the graph and return its RequestInput pause contract."""

    from google.adk.runners import Runner
    from google.genai import types

    resolved_session_id = adk_session_id.strip() or f"hitl_{uuid.uuid4().hex[:16]}"
    interrupt_id = f"missionos_approval:{uuid.uuid4().hex[:16]}"
    binding = dict(approval_binding)
    binding.update(
        {
            "interrupt_id": interrupt_id,
            "operator_session_id": operator_session_id,
        }
    )
    session = await session_service.create_session(
        app_name=MISSIONOS_ADK_V2_HITL_APP_NAME,
        user_id=operator_session_id,
        session_id=resolved_session_id,
        state={"missionos_operator_session_id": operator_session_id},
    )
    workflow = build_missionos_canonical_approval_hitl_workflow(
        approval_validator=approval_validator,
    )
    runner = Runner(
        agent=workflow,
        app_name=MISSIONOS_ADK_V2_HITL_APP_NAME,
        session_service=session_service,
    )
    events: list[Any] = []
    content = types.Content(
        role="user",
        parts=[types.Part(text=json.dumps(binding, ensure_ascii=False, sort_keys=True))],
    )
    async for event in runner.run_async(
        user_id=operator_session_id,
        session_id=session.id,
        new_message=content,
    ):
        events.append(event)
    pending = _pending_request_input(events)
    if not pending:
        return _blocked_result(["adk_request_input_pause_not_created"])
    args = pending.get("args") if isinstance(pending.get("args"), Mapping) else {}
    return {
        "schema_version": MISSIONOS_ADK_V2_HITL_RESULT_SCHEMA_VERSION,
        "hitl_status": "awaiting_canonical_approval",
        "blocking_reasons": [],
        "adk_session_id": session.id,
        "operator_session_id": operator_session_id,
        "interrupt_id": pending["interrupt_id"],
        "request_input_message": args.get("message"),
        "request_input_payload": dict(args.get("payload") or {}),
        "approval_ref": binding.get("approval_ref"),
        "canonical_approval_validated": False,
        "human_input_received": False,
        "human_input_created_authority": False,
        "workflow_name": MISSIONOS_ADK_V2_HITL_WORKFLOW_NAME,
        "session_backend": _session_backend_name(session_service),
        **_authority_floor(),
    }


async def resume_missionos_canonical_approval_hitl(
    *,
    session_service: Any,
    operator_session_id: str,
    adk_session_id: str,
    human_response: Mapping[str, Any],
    approval_validator: CanonicalApprovalValidator,
) -> dict[str, Any]:
    """Resume only when the response contains the expected canonical ref."""

    from google.adk.runners import Runner
    from google.genai import types

    approval_ref = str(human_response.get("approval_ref") or "").strip()
    if not approval_ref:
        blocked = _blocked_result(
            ["adk_request_input_approval_ref_required"],
            human_input_received=True,
        )
        blocked.update(
            {
                "adk_session_id": adk_session_id,
                "operator_session_id": operator_session_id,
                "resume_attempted": False,
                "checkpoint_restored": False,
                "session_backend": _session_backend_name(session_service),
            }
        )
        return blocked
    session = await session_service.get_session(
        app_name=MISSIONOS_ADK_V2_HITL_APP_NAME,
        user_id=operator_session_id,
        session_id=adk_session_id,
    )
    if session is None:
        blocked = _blocked_result(
            ["adk_v2_hitl_session_not_found"],
            approval_ref=approval_ref,
            human_input_received=True,
        )
        blocked.update(
            {
                "adk_session_id": adk_session_id,
                "operator_session_id": operator_session_id,
                "resume_attempted": False,
                "checkpoint_restored": False,
                "session_backend": _session_backend_name(session_service),
            }
        )
        return blocked
    pending = _pending_request_input(list(session.events))
    if not pending:
        blocked = _blocked_result(
            ["adk_v2_hitl_pending_interrupt_not_found"],
            approval_ref=approval_ref,
            human_input_received=True,
        )
        blocked.update(
            {
                "adk_session_id": adk_session_id,
                "operator_session_id": operator_session_id,
                "resume_attempted": False,
                "checkpoint_restored": True,
                "session_backend": _session_backend_name(session_service),
            }
        )
        return blocked
    pending_args = (
        pending.get("args") if isinstance(pending.get("args"), Mapping) else {}
    )
    pending_payload = (
        pending_args.get("payload")
        if isinstance(pending_args.get("payload"), Mapping)
        else {}
    )
    expected_approval_ref = str(pending_payload.get("approval_ref") or "").strip()
    if not expected_approval_ref:
        blocked = _blocked_result(
            ["adk_v2_hitl_checkpoint_approval_ref_missing"],
            approval_ref=approval_ref,
            human_input_received=True,
        )
        blocked.update(
            {
                "adk_session_id": adk_session_id,
                "operator_session_id": operator_session_id,
                "resume_attempted": False,
                "checkpoint_restored": True,
                "session_backend": _session_backend_name(session_service),
            }
        )
        return blocked
    if approval_ref != expected_approval_ref:
        blocked = _blocked_result(
            ["adk_request_input_approval_ref_mismatch"],
            approval_ref=approval_ref,
            human_input_received=True,
        )
        blocked.update(
            {
                "adk_session_id": adk_session_id,
                "operator_session_id": operator_session_id,
                "resume_attempted": False,
                "checkpoint_restored": True,
                "session_backend": _session_backend_name(session_service),
            }
        )
        return blocked
    workflow = build_missionos_canonical_approval_hitl_workflow(
        approval_validator=approval_validator,
    )
    runner = Runner(
        agent=workflow,
        app_name=MISSIONOS_ADK_V2_HITL_APP_NAME,
        session_service=session_service,
    )
    response = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=pending["interrupt_id"],
                    name="adk_request_input",
                    response={"approval_ref": approval_ref},
                )
            )
        ],
    )
    final_output: dict[str, Any] = {}
    async for event in runner.run_async(
        user_id=operator_session_id,
        session_id=adk_session_id,
        new_message=response,
    ):
        if isinstance(event.output, Mapping):
            final_output = dict(event.output)
    if final_output.get("schema_version") != MISSIONOS_ADK_V2_HITL_RESULT_SCHEMA_VERSION:
        final_output = _blocked_result(
            ["adk_v2_hitl_final_output_missing"],
            approval_ref=approval_ref,
            human_input_received=True,
        )
    final_output.update(
        {
            "adk_session_id": adk_session_id,
            "operator_session_id": operator_session_id,
            "interrupt_id": pending["interrupt_id"],
            "resume_attempted": True,
            "checkpoint_restored": True,
            "session_backend": _session_backend_name(session_service),
        }
    )
    return final_output


__all__ = [
    "MISSIONOS_ADK_V2_HITL_APP_NAME",
    "MISSIONOS_ADK_V2_HITL_ENV",
    "MISSIONOS_ADK_V2_HITL_RESULT_SCHEMA_VERSION",
    "MISSIONOS_ADK_V2_HITL_WORKFLOW_NAME",
    "build_missionos_canonical_approval_hitl_workflow",
    "resume_missionos_canonical_approval_hitl",
    "start_missionos_canonical_approval_hitl",
]
