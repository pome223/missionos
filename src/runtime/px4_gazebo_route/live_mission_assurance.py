"""Same-runtime Recovery-Agent-first PX4 Mission Assurance adapter.

The existing Runtime Recovery Agent proposes one concrete recovery candidate.
The shared MissionAssuranceAgent then judges that proposal in the wider mission
context after source Action Feasibility has been materialized.  An accepted
action is revalidated against fresh PX4 evidence.  Approval and dispatch remain
owned by existing runtime boundaries.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from missionos_core import FeasibilityStatus, RevalidationArtifact

from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    MissionResponseProposal,
    MissionSituation,
    configured_mission_assurance_agent,
    persist_mission_assurance_evaluation,
)
from src.intelligence.missionos_agent_runtime import (
    run_missionos_runtime_recovery_agent,
)
from src.intelligence.missionos_mission_incident_graph import (
    run_missionos_mission_incident_graph,
)
from src.runtime.px4_gazebo_route.action_feasibility import (
    action_feasibility_hash_matches,
)
from src.runtime.px4_gazebo_route.core_action_feasibility_adapter import (
    build_runtime_recovery_hazard_state,
    compare_px4_telemetry_cursors,
    verify_runtime_recovery_action_feasibility,
)
from src.runtime.px4_gazebo_route.hazard_state import recovery_policy_sha256
from src.runtime.px4_gazebo_route.mission_assurance_adapter import (
    compile_mission_response_proposal,
)
from src.runtime.px4_gazebo_route.recovery_policy import (
    live_sitl_recovery_policy,
)
from src.runtime.runtime_claim_evidence import (
    RuntimeClaimValidationError,
    validate_runtime_invocation_evidence,
)

LIVE_MISSION_ASSURANCE_SCHEMA_VERSION = "missionos_px4_live_mission_assurance_guard.v2"
LIVE_MISSION_ASSURANCE_MAX_AGE_SECONDS = 30.0
RECOVERY_AGENT_NAME = "missionos_runtime_recovery_agent"
MISSION_ASSURANCE_AGENT_NAME = "mission_assurance_agent"
MISSION_ASSURANCE_CONTEXT_JSON_ENV = "MISSIONOS_MISSION_ASSURANCE_CONTEXT_JSON"
_RECOVERY_NO_DISPATCH_RESPONSES = {
    "continue": "continue",
    "hold": "hold",
    "operator_review": "operator_escalation",
}
_MISSION_CONTEXT_SECTIONS = (
    "mission_contract",
    "progress",
    "observations",
    "constraints",
    "uncertainty",
)
_FORBIDDEN_MISSION_CONTEXT_KEYS = frozenset(
    {
        "approved",
        "approval_granted",
        "operator_approved",
        "approval_recorded",
        "dispatch_authority_created",
        "dispatch_request_sent",
        "command_ack_observed",
        "runtime_progress_observed",
        "landing_observed",
        "delivery_completion_claimed",
        "physical_execution_invoked",
        "gate_passed",
        "progress_counted",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_mission_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep mission facts separate from approval and execution authority."""

    context = _mapping(value)
    unsupported = sorted(set(context) - {*_MISSION_CONTEXT_SECTIONS, "source_refs"})
    if unsupported:
        raise ValueError(
            "mission_assurance_context_sections_unsupported:" + ",".join(unsupported)
        )

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            forbidden = sorted(
                str(key)
                for key in item
                if str(key) in _FORBIDDEN_MISSION_CONTEXT_KEYS
            )
            if forbidden:
                raise ValueError(
                    "mission_assurance_context_authority_keys_forbidden:"
                    + ",".join(forbidden)
                )
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(context)
    return context


def configured_mission_assurance_context() -> dict[str, Any]:
    """Load an optional source-bound mission context for the runtime process."""

    raw = os.environ.get(MISSION_ASSURANCE_CONTEXT_JSON_ENV, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("mission_assurance_context_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise ValueError("mission_assurance_context_json_must_be_object")
    return _validate_mission_context(value)


def horizontal_route_mission_assurance_policy(
    route: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Bind the shared SITL policy to the declared low-altitude route limits."""

    route_payload = (
        route.model_dump(mode="json") if hasattr(route, "model_dump") else _mapping(route)
    )
    policy = live_sitl_recovery_policy()
    route_id = str(route_payload.get("route_plan_id") or "unbound_route")
    policy.update(
        {
            "policy_ref": (f"mission_assurance_px4_horizontal_route_policy:{route_id}"),
            "base_policy_ref": policy.get("policy_ref"),
            "min_terrain_clearance_m": float(route_payload.get("altitude_min_m") or 0.0),
            "battery_return_threshold_percent": max(
                float(policy.get("battery_return_threshold_percent") or 0.0),
                float(route_payload.get("min_battery_margin_pct") or 0.0),
            ),
            "route_plan_id": route_id,
            "route_policy_conversion": ("declared_route_altitude_and_battery_limits.v1"),
        }
    )
    return policy


def _bind_feasibility_context(
    feasibility: Mapping[str, Any],
    *,
    situation_input_digest: str,
    runtime_invocation_evidence: Mapping[str, Any],
    evaluated_at: datetime,
) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in feasibility.items()
        if key not in {"action_feasibility_id", "action_feasibility_sha256"}
    }
    payload.update(
        {
            "mission_situation_input_digest": situation_input_digest,
            "execution_scope": "simulator",
            "evaluated_at": evaluated_at.isoformat(),
            "freshness_deadline": (
                evaluated_at + timedelta(seconds=LIVE_MISSION_ASSURANCE_MAX_AGE_SECONDS)
            ).isoformat(),
            "runtime_invocation_evidence": dict(runtime_invocation_evidence),
        }
    )
    digest = _canonical_sha256(payload)
    return {
        **payload,
        "action_feasibility_sha256": digest,
        "action_feasibility_id": f"action_feasibility_{digest[:12]}",
    }


def _snapshot_bundle(
    observer: Callable[[str], Mapping[str, Any]],
    phase: str,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    bundle = _mapping(observer(phase))
    telemetry = _mapping(bundle.get("telemetry_snapshot"))
    reasons: list[str] = []
    if not telemetry:
        reasons.append(f"mission_assurance_{phase}_telemetry_missing")
    try:
        runtime_evidence = validate_runtime_invocation_evidence(
            bundle.get("runtime_invocation_evidence")
        )
    except RuntimeClaimValidationError as exc:
        runtime_evidence = {}
        reasons.append(f"mission_assurance_{phase}_runtime_evidence_invalid:{exc}")
    if runtime_evidence.get("invocation_exit_code") != 0:
        reasons.append(f"mission_assurance_{phase}_runtime_evidence_exit_nonzero")
    return telemetry, runtime_evidence, reasons


def _runtime_recovery_proposal(
    *,
    task_id: str,
    deviation: Mapping[str, Any],
    telemetry: Mapping[str, Any],
    route: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    available_recovery_executor_action: str,
    recovery_agent_runner: Callable[..., Mapping[str, Any]],
    mission_context: Mapping[str, Any] | None,
    recovery_result: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    shared_context = _validate_mission_context(mission_context)
    result = _mapping(recovery_result)
    if not result:
        result = _mapping(recovery_agent_runner(
            telemetry_snapshot=telemetry,
            mission_context={
                "task_id": task_id,
                "mission_phase": "route_deviation_recovery",
                "route_plan_id": route.get("route_plan_id"),
                "route_deviation": dict(deviation),
                "recovery_trigger": {
                    "trigger_kind": "route_deviation",
                    "decision_scope": "vehicle_recovery_candidate_only",
                    "mission_alignment_deferred_to": MISSION_ASSURANCE_AGENT_NAME,
                    "source_action_feasibility_materialized_after_proposal": True,
                    "absence_of_preselected_candidate_is_not_rejection_evidence": True,
                    "available_executor_action": (
                        "return_to_launch"
                        if available_recovery_executor_action == "rtl"
                        else available_recovery_executor_action
                    ),
                    "selection_instruction": (
                        "independently judge whether a vehicle-level recovery "
                        "candidate is needed; do not decide final mission-level "
                        "alignment, and do not treat the available executor action "
                        "as a requested action"
                    ),
                    "approval_created": False,
                    "dispatch_authority_created": False,
                },
                "mission_contract": _mapping(
                    shared_context.get("mission_contract")
                ),
                "progress": _mapping(shared_context.get("progress")),
                "observations": _mapping(
                    shared_context.get("observations")
                ),
                "constraints": _mapping(shared_context.get("constraints")),
                "uncertainty": _mapping(shared_context.get("uncertainty")),
                "source_refs": [
                    str(item) for item in shared_context.get("source_refs") or ()
                ],
            },
            recovery_policy=recovery_policy,
        ))
    assessment = _mapping(result.get("assessment"))
    agent_output = _mapping(result.get("agent_output"))
    invocations = [
        dict(item)
        for item in result.get("agent_invocations") or []
        if isinstance(item, Mapping)
    ]
    invocation = invocations[-1] if invocations else {}
    selected_action = str(assessment.get("selected_bounded_action") or "").strip()
    parameters = _mapping(assessment.get("proposed_parameters"))
    source_action_feasibility = _mapping(assessment.get("action_feasibility"))
    proposal_identity = {
        "task_id": task_id,
        "selected_bounded_action": selected_action,
        "proposed_parameters": parameters,
        "result_schema_version": result.get("schema_version"),
        "invocation_response_sha256": invocation.get("response_sha256"),
    }
    proposal_digest = _canonical_sha256(proposal_identity)
    proposal = {
        "proposal_ref": f"runtime_recovery_agent_proposal:{proposal_digest[:12]}",
        "agent_name": RECOVERY_AGENT_NAME,
        "runtime_status": result.get("runtime_status"),
        "selected_bounded_action": selected_action,
        "proposed_parameters": parameters,
        "rationale": str(agent_output.get("rationale") or ""),
        "expected_outcome": str(agent_output.get("expected_outcome") or ""),
        "operator_instruction": str(agent_output.get("operator_instruction") or ""),
        "operator_approval_required": assessment.get("operator_approval_required") is True,
        "source_action_feasibility": source_action_feasibility,
        "model_inference_invoked": bool(
            invocations and invocation.get("agent_name") == RECOVERY_AGENT_NAME
        ),
        "model_invocation_evidence": {
            key: invocation.get(key)
            for key in (
                "agent_name",
                "agent_role",
                "provider",
                "invocation_kind",
                "model_id",
                "prompt_sha256",
                "response_sha256",
                "invocation_started_at",
                "invocation_completed_at",
                "function_tool_called",
            )
        },
        "source_result_sha256": _canonical_sha256(result),
        "approval_recorded": False,
        "dispatch_authority_created": False,
        "dispatch_request_sent": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }
    reasons: list[str] = []
    if result.get("runtime_status") != "proposal_guardrail_passed":
        reasons.append("runtime_recovery_agent_proposal_not_accepted")
        reasons.extend(str(item) for item in result.get("blocking_reasons") or [])
    if not proposal["model_inference_invoked"]:
        reasons.append("runtime_recovery_agent_model_inference_not_observed")
    if not selected_action:
        reasons.append("runtime_recovery_agent_action_missing")
    if selected_action not in _RECOVERY_NO_DISPATCH_RESPONSES:
        if source_action_feasibility.get("action") != selected_action:
            reasons.append("runtime_recovery_agent_action_feasibility_action_mismatch")
        if source_action_feasibility.get("feasibility_status") != "verified_feasible":
            reasons.append("runtime_recovery_agent_action_feasibility_not_verified")
    if assessment.get("backend_action_request_allowed") is not False:
        reasons.append("runtime_recovery_agent_backend_action_authority_invalid")
    for key in (
        "dispatch_authority_created",
        "physical_execution_invoked",
        "progress_counted",
    ):
        if assessment.get(key) is not False:
            reasons.append(f"runtime_recovery_agent_{key}_must_be_false")
    return result, proposal, list(dict.fromkeys(reasons))


def _feasibility(
    *,
    telemetry: Mapping[str, Any],
    runtime_evidence: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    candidate: Mapping[str, Any],
    situation_input_digest: str,
    prior_cursor: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evaluated_at = datetime.now(timezone.utc)
    hazard = build_runtime_recovery_hazard_state(
        telemetry_snapshot=telemetry,
        recovery_policy=recovery_policy,
        observed_at=str(telemetry.get("observed_at") or evaluated_at.isoformat()),
        prior_telemetry_cursor=prior_cursor,
        expected_policy_sha256=recovery_policy_sha256(recovery_policy),
    )
    result = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=hazard,
        recovery_policy=recovery_policy,
    )
    return hazard, _bind_feasibility_context(
        result,
        situation_input_digest=situation_input_digest,
        runtime_invocation_evidence=runtime_evidence,
        evaluated_at=evaluated_at,
    )


def _source_feasibility(
    *,
    telemetry: Mapping[str, Any],
    runtime_evidence: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], datetime]:
    """Evaluate the Recovery candidate before Assurance sees the situation."""

    evaluated_at = (
        _parse_timestamp(runtime_evidence.get("invocation_completed_at"))
        or datetime.now(timezone.utc)
    )
    hazard = build_runtime_recovery_hazard_state(
        telemetry_snapshot=telemetry,
        recovery_policy=recovery_policy,
        observed_at=str(telemetry.get("observed_at") or evaluated_at.isoformat()),
        expected_policy_sha256=recovery_policy_sha256(recovery_policy),
    )
    raw = verify_runtime_recovery_action_feasibility(
        candidate=candidate,
        hazard_state=hazard,
        recovery_policy=recovery_policy,
    )
    return hazard, raw, evaluated_at


def _source_feasibility_reasons(
    *,
    feasibility: Mapping[str, Any],
    expected_action: str,
    expected_parameters: Mapping[str, Any],
    situation_input_digest: str,
) -> list[str]:
    reasons: list[str] = []
    if not action_feasibility_hash_matches(feasibility):
        reasons.append("mission_assurance_source_feasibility_hash_mismatch")
    if feasibility.get("feasibility_status") != "verified_feasible":
        reasons.append("mission_assurance_source_feasibility_not_verified")
        reasons.extend(
            str(item)
            for item in (
                list(feasibility.get("blocking_reasons") or [])
                + list(feasibility.get("unverified_reasons") or [])
            )
        )
    if feasibility.get("action") != expected_action:
        reasons.append("mission_assurance_source_feasibility_action_mismatch")
    if _mapping(feasibility.get("candidate_parameters")) != dict(expected_parameters):
        reasons.append("mission_assurance_source_feasibility_parameters_mismatch")
    if feasibility.get("mission_situation_input_digest") != situation_input_digest:
        reasons.append("mission_assurance_source_feasibility_situation_digest_mismatch")
    if feasibility.get("execution_scope") != "simulator":
        reasons.append("mission_assurance_source_feasibility_execution_scope_mismatch")
    for key in (
        "approval_created",
        "dispatch_authority_created",
        "physical_execution_invoked",
        "completion_claimed",
        "progress_counted",
    ):
        if feasibility.get(key) is not False:
            reasons.append(f"mission_assurance_source_feasibility_{key}_must_be_false")
    return list(dict.fromkeys(reasons))


def _revalidation_reasons(
    *,
    original: Mapping[str, Any],
    current: Mapping[str, Any],
    expected_action: str,
    expected_parameters: Mapping[str, Any],
    situation_input_digest: str,
) -> list[str]:
    reasons: list[str] = []
    for label, feasibility in (("original", original), ("current", current)):
        if not action_feasibility_hash_matches(feasibility):
            reasons.append(f"mission_assurance_{label}_feasibility_hash_mismatch")
        if feasibility.get("feasibility_status") != "verified_feasible":
            reasons.append(f"mission_assurance_{label}_feasibility_not_verified")
            reasons.extend(
                str(item)
                for item in (
                    list(feasibility.get("blocking_reasons") or [])
                    + list(feasibility.get("unverified_reasons") or [])
                )
            )
        if feasibility.get("action") != expected_action:
            reasons.append(f"mission_assurance_{label}_action_mismatch")
        if _mapping(feasibility.get("candidate_parameters")) != dict(expected_parameters):
            reasons.append(f"mission_assurance_{label}_parameters_mismatch")
        if feasibility.get("mission_situation_input_digest") != situation_input_digest:
            reasons.append(f"mission_assurance_{label}_situation_digest_mismatch")
        if feasibility.get("execution_scope") != "simulator":
            reasons.append(f"mission_assurance_{label}_execution_scope_mismatch")
        for key in (
            "approval_created",
            "dispatch_authority_created",
            "physical_execution_invoked",
            "completion_claimed",
            "progress_counted",
        ):
            if feasibility.get(key) is not False:
                reasons.append(f"mission_assurance_{label}_{key}_must_be_false")

    if original.get("action_feasibility_sha256") == current.get("action_feasibility_sha256"):
        reasons.append("mission_assurance_feasibility_not_recomputed")
    if original.get("policy_sha256") != current.get("policy_sha256"):
        reasons.append("mission_assurance_policy_drift")
    original_models = _mapping(original.get("model_refs"))
    current_models = _mapping(current.get("model_refs"))
    if not original_models or not current_models:
        reasons.append("mission_assurance_model_binding_missing")
    elif original_models != current_models:
        reasons.append("mission_assurance_model_drift")
    cursor_order = compare_px4_telemetry_cursors(
        _mapping(original.get("telemetry_cursor")),
        _mapping(current.get("telemetry_cursor")),
    )
    if cursor_order.value != "before":
        reasons.append(f"mission_assurance_current_cursor_not_advanced:{cursor_order.value}")

    now = datetime.now(timezone.utc)
    current_evaluated_at = _parse_timestamp(current.get("evaluated_at"))
    current_deadline = _parse_timestamp(current.get("freshness_deadline"))
    current_runtime_evidence = _mapping(current.get("runtime_invocation_evidence"))
    current_runtime_completed_at = _parse_timestamp(
        current_runtime_evidence.get("invocation_completed_at")
    )
    if current_runtime_completed_at is None:
        reasons.append("mission_assurance_current_runtime_timestamp_invalid")
    else:
        runtime_age = (now - current_runtime_completed_at).total_seconds()
        if runtime_age < -1.0 or runtime_age > LIVE_MISSION_ASSURANCE_MAX_AGE_SECONDS:
            reasons.append("mission_assurance_current_runtime_evidence_stale")
    if current_evaluated_at is None:
        reasons.append("mission_assurance_current_evaluated_at_invalid")
    else:
        age = (now - current_evaluated_at).total_seconds()
        if age < -1.0 or age > LIVE_MISSION_ASSURANCE_MAX_AGE_SECONDS:
            reasons.append("mission_assurance_current_feasibility_stale")
        if (
            current_runtime_completed_at is not None
            and current_evaluated_at < current_runtime_completed_at
        ):
            reasons.append("mission_assurance_current_evaluated_before_runtime_completed")
    if current_deadline is None or now > current_deadline:
        reasons.append("mission_assurance_current_freshness_deadline_expired")
    return list(dict.fromkeys(reasons))


def evaluate_live_route_deviation(
    *,
    task_id: str,
    artifact_dir: Path | str,
    route: Mapping[str, Any] | Any,
    deviation: Mapping[str, Any],
    available_recovery_executor_action: str,
    operator_preapproval_observed: bool,
    telemetry_observer: Callable[[str], Mapping[str, Any]],
    agent: MissionAssuranceAgent | None = None,
    recovery_agent_runner: Callable[..., Mapping[str, Any]] = (
        run_missionos_runtime_recovery_agent
    ),
    recovery_policy: Mapping[str, Any] | None = None,
    mission_context: Mapping[str, Any] | None = None,
    operator_recovery_approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return eligibility after Recovery proposal, Assurance judgment, and checks."""

    root = Path(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    route_payload = (
        route.model_dump(mode="json") if hasattr(route, "model_dump") else _mapping(route)
    )
    policy = dict(recovery_policy or horizontal_route_mission_assurance_policy(route_payload))
    policy_digest = recovery_policy_sha256(policy)
    original_telemetry, original_runtime_evidence, blocking_reasons = _snapshot_bundle(
        telemetry_observer, "original"
    )
    graph_recovery: dict[str, Any] = {}

    def run_recovery_node(**_: Any) -> Mapping[str, Any]:
        recovery_tuple = _runtime_recovery_proposal(
            task_id=task_id,
            deviation=deviation,
            telemetry=original_telemetry,
            route=route_payload,
            recovery_policy=policy,
            available_recovery_executor_action=(
                available_recovery_executor_action
            ),
            recovery_agent_runner=recovery_agent_runner,
            mission_context=mission_context,
        )
        graph_recovery["tuple"] = recovery_tuple
        return recovery_tuple[0]

    shared_context = _validate_mission_context(mission_context)
    incident_graph = run_missionos_mission_incident_graph(
        telemetry_snapshot=original_telemetry,
        mission_context={
            "task_id": task_id,
            "mission_phase": "route_deviation_recovery",
            "execution_scope": "simulator",
            "mission_contract": _mapping(
                shared_context.get("mission_contract")
            ),
            "progress": _mapping(shared_context.get("progress")),
            "observations": {
                "route_deviation": dict(deviation),
                **_mapping(shared_context.get("observations")),
            },
            "constraints": _mapping(shared_context.get("constraints")),
            "uncertainty": _mapping(shared_context.get("uncertainty")),
            "source_refs": [
                str(item) for item in shared_context.get("source_refs") or ()
            ],
        },
        recovery_policy=policy,
        recovery_runner=run_recovery_node,
        mission_assurance_agent=(
            agent or configured_mission_assurance_agent()
        ),
    )
    recovery_tuple = graph_recovery.get("tuple")
    if not isinstance(recovery_tuple, tuple) or len(recovery_tuple) != 3:
        recovery_result = _mapping(incident_graph.get("recovery_result"))
        recovery_proposal = {}
        recovery_reasons = [
            "mission_incident_graph_recovery_projection_missing"
        ]
    else:
        recovery_result, recovery_proposal, recovery_reasons = recovery_tuple
    blocking_reasons.extend(recovery_reasons)
    recovery_proposal_valid = not recovery_reasons
    recovery_bounded_action = str(
        recovery_proposal.get("selected_bounded_action") or ""
    )
    recovery_no_dispatch_response = _RECOVERY_NO_DISPATCH_RESPONSES.get(
        recovery_bounded_action
    )
    candidate: dict[str, Any] = {}
    original_hazard: dict[str, Any] = {}
    original_feasibility_raw: dict[str, Any] = {}
    original_feasibility: dict[str, Any] = {}
    source_evaluated_at: datetime | None = None
    if recovery_proposal_valid and not recovery_no_dispatch_response:
        candidate = {
            "candidate_id": (
                "mission_assurance_candidate_"
                + str(recovery_proposal.get("proposal_ref") or "missing").split(":")[-1]
            ),
            "selected_bounded_action": recovery_bounded_action,
            "proposed_parameters": _mapping(
                recovery_proposal.get("proposed_parameters")
            ),
            "source_refs": [
                str(recovery_proposal.get("proposal_ref") or "recovery_proposal:missing"),
                f"px4_route_deviation:{task_id}",
                f"px4_same_runtime_telemetry:{original_telemetry.get('sample_index')}",
            ],
        }
        (
            original_hazard,
            original_feasibility_raw,
            source_evaluated_at,
        ) = _source_feasibility(
            telemetry=original_telemetry,
            runtime_evidence=original_runtime_evidence,
            recovery_policy=policy,
            candidate=candidate,
        )
    graph_situation = _mapping(incident_graph.get("mission_situation"))
    if graph_situation:
        situation = MissionSituation.from_dict(graph_situation)
    else:
        blocking_reasons.append("mission_incident_graph_situation_missing")
        diagnostic_source = {
            "task_id": task_id,
            "telemetry_snapshot": original_telemetry,
            "incident_graph": incident_graph,
        }
        diagnostic_digest = _canonical_sha256(diagnostic_source)
        situation = MissionSituation(
            situation_id=f"blocked_mission_situation_{diagnostic_digest[:12]}",
            observed_at=str(
                original_telemetry.get("observed_at")
                or datetime.now(timezone.utc).isoformat()
            ),
            mission_contract={
                "objective": "preserve the declared mission",
                "graph_projection_status": "missing",
            },
            progress={"task_id": task_id},
            observations={"runtime_telemetry": original_telemetry},
            constraints={"mission_incident_graph_required": True},
            uncertainty={"graph_projection_missing": True},
            source_refs=(f"mission_incident_graph:{task_id}",),
            source_schema_version=str(
                incident_graph.get("schema_version") or "missing"
            ),
            input_digest=diagnostic_digest,
            execution_scope="simulator",
            allowed_response_kinds=("operator_escalation",),
        )
    if original_feasibility_raw and source_evaluated_at is not None:
        original_feasibility = _bind_feasibility_context(
            original_feasibility_raw,
            situation_input_digest=situation.input_digest,
            runtime_invocation_evidence=original_runtime_evidence,
            evaluated_at=source_evaluated_at,
        )
        blocking_reasons.extend(
            _source_feasibility_reasons(
                feasibility=original_feasibility,
                expected_action=recovery_bounded_action,
                expected_parameters=_mapping(
                    recovery_proposal.get("proposed_parameters")
                ),
                situation_input_digest=situation.input_digest,
            )
        )
    proposal_payload = _mapping(
        incident_graph.get("mission_assurance_proposal")
    )
    if proposal_payload:
        proposal = MissionResponseProposal.from_dict(proposal_payload)
    else:
        proposal = MissionResponseProposal(
            proposal_id=(
                f"mission_response_proposal_{situation.input_digest[:12]}"
            ),
            generated_at=datetime.now(timezone.utc).isoformat(),
            situation_ref=f"mission_situation:{situation.situation_id}",
            situation_input_digest=situation.input_digest,
            proposed_response_kind="operator_escalation",
            parameters={},
            rationale="The unified mission incident graph blocked before judgment.",
            expected_outcome="A human reviews the source-bound incident.",
            uncertainty="No Mission Assurance action judgment was accepted.",
            operator_question="Review the incident and select the next step.",
            judgment_status="not_invoked",
            judgment_mode="llm_required",
            fallback_mode="operator_escalation_only",
            model_inference_invoked=False,
            model_invocation_evidence={},
            blocking_reasons=tuple(
                str(item)
                for item in incident_graph.get("blocking_reasons") or ()
            ),
        )
    evaluation = persist_mission_assurance_evaluation(
        situation=situation,
        proposal=proposal,
        artifact_root=root,
        artifact_relative=lambda path: path.relative_to(root).as_posix(),
    )
    compilation = compile_mission_response_proposal(proposal)
    no_action_response = compilation.get("compile_status") == "no_action_required"
    if proposal.judgment_status != "proposal_guardrail_passed":
        blocking_reasons.extend(proposal.blocking_reasons)
        blocking_reasons.append("mission_assurance_agent_judgment_not_accepted")
    if compilation.get("compile_status") not in {
        "candidate_compiled",
        "no_action_required",
    }:
        blocking_reasons.extend(compilation.get("blocking_reasons") or [])
        blocking_reasons.append("mission_assurance_action_candidate_not_compiled")

    assurance_bounded_action = str(compilation.get("bounded_action_kind") or "")
    no_dispatch_responses_aligned = bool(
        recovery_proposal_valid
        and no_action_response
        and recovery_no_dispatch_response == proposal.proposed_response_kind
    )
    assurance_prevents_mission_continuation = bool(
        recovery_proposal_valid
        and recovery_no_dispatch_response == "continue"
        and no_action_response
        and proposal.proposed_response_kind in {
            "hold",
            "operator_escalation",
        }
        and proposal.judgment_status == "proposal_guardrail_passed"
        and not proposal.parameters
        and not blocking_reasons
    )
    assurance_action_without_recovery_candidate = bool(
        recovery_proposal_valid
        and recovery_no_dispatch_response is not None
        and compilation.get("compile_status") == "candidate_compiled"
        and assurance_bounded_action
        and proposal.judgment_status == "proposal_guardrail_passed"
        and not proposal.parameters
    )
    no_action_agent_disagreement = bool(
        recovery_proposal_valid
        and recovery_no_dispatch_response is not None
        and no_action_response
        and proposal.proposed_response_kind != recovery_no_dispatch_response
        and not assurance_prevents_mission_continuation
        and proposal.judgment_status == "proposal_guardrail_passed"
        and not proposal.parameters
        and not blocking_reasons
    )
    assurance_suppresses_recovery_proposal = bool(
        recovery_proposal_valid
        and recovery_no_dispatch_response is None
        and recovery_bounded_action
        and not blocking_reasons
        and {"return_to_launch": "rtl"}.get(recovery_bounded_action)
        == available_recovery_executor_action
        and no_action_response
        and proposal.proposed_response_kind in {
            "continue",
            "hold",
            "operator_escalation",
        }
        and proposal.judgment_status == "proposal_guardrail_passed"
        and not proposal.parameters
    )
    if (
        assurance_bounded_action != recovery_bounded_action
        and not no_dispatch_responses_aligned
        and not assurance_prevents_mission_continuation
        and not assurance_suppresses_recovery_proposal
        and not assurance_action_without_recovery_candidate
        and not no_action_agent_disagreement
    ):
        blocking_reasons.append(
            (
                f"mission_assurance_{proposal.proposed_response_kind}_blocks_"
                "recovery_agent_proposal"
            )
            if no_action_response
            else "mission_assurance_response_not_aligned_with_recovery_agent_proposal"
        )
    if proposal.parameters:
        blocking_reasons.append(
            "mission_assurance_parameters_must_not_override_recovery_agent_proposal"
        )
    assurance_accepts_recovery_proposal = (
        recovery_proposal_valid
        and proposal.judgment_status == "proposal_guardrail_passed"
        and (
            (
                compilation.get("compile_status") == "candidate_compiled"
                and assurance_bounded_action == recovery_bounded_action
            )
            or no_dispatch_responses_aligned
        )
        and not proposal.parameters
    )
    bounded_action = (
        recovery_bounded_action
        if assurance_accepts_recovery_proposal and not no_dispatch_responses_aligned
        else ""
    )
    expected_dispatch_action = {
        "return_to_launch": "rtl",
    }.get(bounded_action)
    recovery_approval = _mapping(operator_recovery_approval)
    recovery_approval_observed = bool(
        bounded_action
        and recovery_approval.get("operator_approval_performed") is True
        and recovery_approval.get("approved_recovery_action")
        == expected_dispatch_action
        and recovery_approval.get("explicit_recovery_dispatch_approval") is True
    )
    if (
        bounded_action
        and expected_dispatch_action != available_recovery_executor_action
    ):
        blocking_reasons.append("mission_assurance_compiled_action_outside_operator_upper_bound")
    current_hazard: dict[str, Any] = {}
    current_feasibility: dict[str, Any] = {}
    revalidation_payload: dict[str, Any] = {}
    post_suppression_observation: dict[str, Any] = {}
    if bounded_action and recovery_approval_observed:
        current_telemetry, current_runtime_evidence, current_reasons = _snapshot_bundle(
            telemetry_observer, "current"
        )
        blocking_reasons.extend(current_reasons)
        current_hazard, current_feasibility = _feasibility(
            telemetry=current_telemetry,
            runtime_evidence=current_runtime_evidence,
            recovery_policy=policy,
            candidate=candidate,
            situation_input_digest=situation.input_digest,
            prior_cursor=_mapping(original_feasibility.get("telemetry_cursor")),
        )
        revalidation_reasons = _revalidation_reasons(
            original=original_feasibility,
            current=current_feasibility,
            expected_action=bounded_action,
            expected_parameters=_mapping(
                recovery_proposal.get("proposed_parameters")
            ),
            situation_input_digest=situation.input_digest,
        )
        blocking_reasons.extend(revalidation_reasons)
        current_status = str(current_feasibility.get("feasibility_status") or "unverified")
        core_status = (
            FeasibilityStatus.VERIFIED_FEASIBLE
            if not revalidation_reasons
            else FeasibilityStatus.BLOCKED
            if current_status == "blocked"
            else FeasibilityStatus.UNVERIFIED
        )
        revalidation = RevalidationArtifact(
            proposal_ref=str(compilation.get("proposal_ref") or ""),
            original_result_sha256=str(original_feasibility.get("action_feasibility_sha256") or ""),
            current_result_sha256=str(current_feasibility.get("action_feasibility_sha256") or ""),
            status=core_status,
            reasons=tuple(revalidation_reasons),
        )
        revalidation_payload = {
            **revalidation.to_dict(),
            "revalidation_status": ("valid" if not revalidation_reasons else "blocked"),
            "revalidated_at": datetime.now(timezone.utc).isoformat(),
            "original_telemetry_cursor": original_feasibility.get("telemetry_cursor"),
            "current_telemetry_cursor": current_feasibility.get("telemetry_cursor"),
            "approval_recorded": False,
            "dispatch_request_sent": False,
            "command_ack_observed": False,
            "runtime_progress_observed": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
            "delivery_completion_claimed": False,
        }

    if (
        assurance_suppresses_recovery_proposal
        or assurance_prevents_mission_continuation
    ) and not blocking_reasons:
        (
            post_suppression_telemetry,
            post_suppression_runtime_evidence,
            post_suppression_reasons,
        ) = _snapshot_bundle(telemetry_observer, "post_suppression")
        blocking_reasons.extend(post_suppression_reasons)
        post_suppression_observation = {
            "observation_kind": "mission_assurance_post_suppression_reobservation",
            "telemetry_snapshot": post_suppression_telemetry,
            "runtime_invocation_evidence": post_suppression_runtime_evidence,
            "source_mission_situation_input_digest": situation.input_digest,
            "dispatch_request_sent": False,
            "command_ack_observed": False,
            "runtime_progress_observed": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        }

    blocking_reasons = list(dict.fromkeys(str(item) for item in blocking_reasons))
    # The approval used to start the route is not authority for a new Recovery
    # action. A return judgment therefore stops at a fresh human checkpoint.
    recovery_operator_approval_required = bool(
        not blocking_reasons
        and expected_dispatch_action == available_recovery_executor_action
        and original_feasibility.get("feasibility_status") == "verified_feasible"
        and not recovery_approval_observed
    )
    dispatch_eligible = bool(
        not blocking_reasons
        and recovery_approval_observed
        and expected_dispatch_action == available_recovery_executor_action
        and revalidation_payload.get("revalidation_status") == "valid"
    )
    awaiting_operator_approval = recovery_operator_approval_required
    no_dispatch_accepted = bool(
        (no_dispatch_responses_aligned and assurance_accepts_recovery_proposal)
        or assurance_suppresses_recovery_proposal
        or assurance_prevents_mission_continuation
    ) and not blocking_reasons
    disagreement_escalation_accepted = bool(
        (
            assurance_action_without_recovery_candidate
            or no_action_agent_disagreement
        )
        and not blocking_reasons
    )
    if bounded_action and recovery_approval_observed:
        decision_sequence = [
            RECOVERY_AGENT_NAME,
            "source_action_feasibility",
            MISSION_ASSURANCE_AGENT_NAME,
            "fresh_operator_recovery_approval_observed",
            "dispatch_time_action_feasibility_revalidation",
            "operator_approved_recovery_dispatch_boundary",
        ]
    elif bounded_action:
        decision_sequence = [
            RECOVERY_AGENT_NAME,
            "source_action_feasibility",
            MISSION_ASSURANCE_AGENT_NAME,
            "fresh_operator_recovery_approval_boundary",
        ]
    elif (
        no_dispatch_accepted
        and no_dispatch_responses_aligned
        and proposal.proposed_response_kind == "continue"
    ):
        decision_sequence = [
            RECOVERY_AGENT_NAME,
            MISSION_ASSURANCE_AGENT_NAME,
            "existing_operator_approval_continue_boundary",
        ]
    elif no_dispatch_accepted and assurance_prevents_mission_continuation:
        decision_sequence = [
            RECOVERY_AGENT_NAME,
            MISSION_ASSURANCE_AGENT_NAME,
            "mission_assurance_continuation_suppression_boundary",
            "post_suppression_reobservation",
        ]
    elif disagreement_escalation_accepted:
        decision_sequence = [
            RECOVERY_AGENT_NAME,
            MISSION_ASSURANCE_AGENT_NAME,
            "agent_disagreement_operator_escalation_boundary",
        ]
    elif candidate:
        decision_sequence = [
            RECOVERY_AGENT_NAME,
            "source_action_feasibility",
            MISSION_ASSURANCE_AGENT_NAME,
            "mission_assurance_no_dispatch_boundary",
            "post_suppression_reobservation",
        ]
    else:
        decision_sequence = [
            RECOVERY_AGENT_NAME,
            MISSION_ASSURANCE_AGENT_NAME,
            "mission_assurance_no_dispatch_boundary",
        ]
    result = {
        "schema_version": LIVE_MISSION_ASSURANCE_SCHEMA_VERSION,
        "missionos_mission_incident_graph": incident_graph,
        "guard_status": (
            "dispatch_eligible"
            if dispatch_eligible
            else "awaiting_operator_approval"
            if awaiting_operator_approval
            else "no_dispatch"
            if no_dispatch_accepted
            else "operator_escalation"
            if disagreement_escalation_accepted
            else "blocked"
        ),
        "task_id": task_id,
        "available_recovery_executor_action": available_recovery_executor_action,
        "selected_recovery_action": (
            expected_dispatch_action if dispatch_eligible else None
        ),
        "proposed_recovery_action": (
            expected_dispatch_action if awaiting_operator_approval else None
        ),
        "operator_recovery_approval_request": (
            {
                "schema_version": "missionos_mission_assurance_recovery_approval_request.v1",
                "request_status": "awaiting_operator_approval",
                "recovery_action": expected_dispatch_action,
                "requires_new_human_approval": True,
                "route_execution_approval_is_not_recovery_approval": True,
                "recovery_proposal_ref": recovery_proposal.get("proposal_ref"),
                "mission_situation_input_digest": situation.input_digest,
                "source_action_feasibility_sha256": original_feasibility.get(
                    "action_feasibility_sha256"
                ),
                "mission_assurance_proposal_ref": compilation.get("proposal_ref"),
                "mission_assurance_response_kind": proposal.proposed_response_kind,
                "approval_recorded": False,
                "dispatch_authority_created": False,
                "dispatch_request_sent": False,
                "physical_execution_invoked": False,
            }
            if awaiting_operator_approval
            else {}
        ),
        "route_execution_approval_observed": operator_preapproval_observed,
        "operator_recovery_approval_observed": recovery_approval_observed,
        "decision_input_bindings": {
            "mission_situation_input_digest": situation.input_digest,
            "recovery_proposal_ref": recovery_proposal.get("proposal_ref"),
            "recovery_proposal_source_result_sha256": recovery_proposal.get(
                "source_result_sha256"
            ),
            "source_action_feasibility_sha256": original_feasibility.get(
                "action_feasibility_sha256"
            ),
            "source_telemetry_cursor": original_feasibility.get("telemetry_cursor"),
            "policy_sha256": policy_digest,
            "available_recovery_executor_action": available_recovery_executor_action,
            "route_execution_approval_observed": operator_preapproval_observed,
            "operator_recovery_approval_observed": recovery_approval_observed,
        },
        "mission_assurance_raw_response_sha256": proposal.model_invocation_evidence.get(
            "response_sha256"
        ),
        "decision_sequence": decision_sequence,
        "runtime_recovery_agent_invoked": recovery_proposal.get(
            "model_inference_invoked"
        )
        is True,
        "recovery_agent_invoked_before_mission_assurance": True,
        "runtime_recovery_agent_result": recovery_result,
        "runtime_recovery_agent_proposal": recovery_proposal,
        "recovery_proposed_action_feasibility": recovery_proposal.get(
            "source_action_feasibility"
        ),
        "mission_assurance_evaluation": evaluation,
        "mission_assurance_response_kind": proposal.proposed_response_kind,
        "recovery_proposal_accepted": assurance_accepts_recovery_proposal,
        "recovery_no_dispatch_response_accepted": bool(
            no_dispatch_responses_aligned and no_dispatch_accepted
        ),
        "mission_assurance_suppression_accepted": bool(
            assurance_suppresses_recovery_proposal and no_dispatch_accepted
        ),
        "agent_disagreement_observed": disagreement_escalation_accepted,
        "agent_disagreement_kind": (
            "assurance_action_without_recovery_action_candidate"
            if assurance_action_without_recovery_candidate
            else "mission_no_action_response_disagreement"
            if no_action_agent_disagreement
            else None
        ),
        "agent_disagreement_resolution": (
            "operator_escalation"
            if disagreement_escalation_accepted
            else None
        ),
        "assurance_requested_action": (
            assurance_bounded_action
            if assurance_action_without_recovery_candidate
            else None
        ),
        "recovery_no_action_response": (
            recovery_no_dispatch_response
            if disagreement_escalation_accepted
            else None
        ),
        "dispatch_prevented_by_mission_assurance": (
            assurance_suppresses_recovery_proposal and no_dispatch_accepted
        ),
        "mission_continuation_prevented_by_mission_assurance": (
            assurance_prevents_mission_continuation and no_dispatch_accepted
        ),
        "suppression_source": (
            MISSION_ASSURANCE_AGENT_NAME
            if (
                assurance_suppresses_recovery_proposal
                or assurance_prevents_mission_continuation
            )
            else None
        ),
        "suppression_reason": (
            f"mission_assurance_{proposal.proposed_response_kind}_suppressed_"
            "feasible_recovery_proposal"
            if assurance_suppresses_recovery_proposal
            else f"mission_assurance_{proposal.proposed_response_kind}_prevented_"
            "mission_continuation"
            if assurance_prevents_mission_continuation
            else None
        ),
        "suppressed_recovery_action": (
            recovery_bounded_action if assurance_suppresses_recovery_proposal else None
        ),
        "suppressed_recovery_response": (
            recovery_no_dispatch_response
            if assurance_prevents_mission_continuation
            else None
        ),
        "response_compilation": compilation,
        "recovery_policy_ref": policy.get("policy_ref"),
        "recovery_policy_sha256": policy_digest,
        "original_hazard_state": original_hazard,
        "original_action_feasibility": original_feasibility,
        "current_hazard_state": current_hazard,
        "current_action_feasibility": current_feasibility,
        "action_revalidation": revalidation_payload,
        "post_suppression_observation": post_suppression_observation,
        "blocking_reasons": blocking_reasons,
        "approval_recorded": False,
        "dispatch_authority_created": False,
        "dispatch_request_sent": False,
        "command_ack_observed": False,
        "runtime_progress_observed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
        "delivery_completion_claimed": False,
    }
    artifact_root = root / "missionos_px4_live_mission_assurance"
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_root / "missionos_px4_live_mission_assurance.json"
    artifact_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return {
        **result,
        "artifact_path": artifact_path.relative_to(root).as_posix(),
    }


__all__ = [
    "LIVE_MISSION_ASSURANCE_MAX_AGE_SECONDS",
    "LIVE_MISSION_ASSURANCE_SCHEMA_VERSION",
    "MISSION_ASSURANCE_CONTEXT_JSON_ENV",
    "configured_mission_assurance_context",
    "evaluate_live_route_deviation",
    "horizontal_route_mission_assurance_policy",
]
