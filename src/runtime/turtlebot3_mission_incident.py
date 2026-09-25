"""Nav2 evidence adapter for the shared Mission Incident workflow.

No mission-level judgment or approval policy is implemented here. The adapter
binds the existing Recovery result and Nav2 candidate to the common LLM graph.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from src.intelligence.missionos_mission_incident_continuation_graph import (
    _frozen_graph_reasons,
    run_missionos_mission_incident_continuation_graph,
)
from src.intelligence.missionos_mission_incident_graph import (
    run_missionos_mission_incident_graph,
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def checkpoint_candidate(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    """The exact compiled candidate judged and later approved, including yaw."""
    return {
        key: checkpoint.get(key)
        for key in (
            "proposal_id",
            "recovery_proposal_id",
            "selected_action",
            "approved_parameters",
            "recovery_goal_poses",
            "recovery_candidate_binding",
            "planned_segments_sha256",
            "resume_state_hash",
        )
    }


def navigation_telemetry_from_evaluation(
    evaluation: Mapping[str, Any], *, candidates: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """Project an actual plan-only observation, never invent its capture time.

    Costmap identity and every selected path point remain part of the predicted
    state. A fresh planning observation that changes either needs new judgment.
    """
    valid = bool(
        all(
            isinstance(evaluation.get(key), str)
            and re.fullmatch(r"[0-9a-f]{64}", evaluation[key])
            for key in ("global_costmap_content_sha256", "local_costmap_content_sha256")
        )
        and candidates
        and all(
            isinstance(candidate.get("path_points"), list)
            and len(candidate["path_points"]) >= 2
            and all(
                isinstance(point, Mapping)
                and all(type(point.get(key)) in (int, float) and math.isfinite(point[key])
                        for key in ("x_m", "y_m"))
                for point in candidate["path_points"]
            )
            for candidate in candidates
        )
    )
    return {
        "observed_at": evaluation.get("observation_captured_at") if valid else None,
        "navigation_state": {
            "observation_contract_valid": valid,
            "global_costmap_content_sha256": evaluation.get("global_costmap_content_sha256"),
            "local_costmap_content_sha256": evaluation.get("local_costmap_content_sha256"),
            "candidate_paths": [
                {key: candidate[key] for key in (
                    "candidate_id", "x_m", "y_m", "yaw_rad", "path_points", "path_sha256",
                    "path_length_m", "maximum_path_cost", "local_maximum_path_cost",
                    "minimum_clearance_m", "local_minimum_clearance_m",
                ) if key in candidate}
                for candidate in candidates
            ],
        },
    }


def judge_turtlebot3_checkpoint(
    *,
    checkpoint: Mapping[str, Any],
    proposal: Mapping[str, Any],
    recovery_proposal: Mapping[str, Any],
    planner_result: Mapping[str, Any],
    motion: Mapping[str, Any],
    obstacle: Mapping[str, Any],
    mission_assurance_agent: Any = None,
) -> dict[str, Any]:
    from src.runtime.turtlebot3_assurance_policy import mission_contract

    action = str(checkpoint.get("selected_action") or "")
    if recovery_proposal.get("proposal_source") == "operator":
        from src.intelligence.turtlebot3_recovery_planner import run_turtlebot3_recovery_planner

        # A changed operator candidate needs fresh Recovery evidence and a fresh
        # Assurance judgment. Neither may be inherited from the old parameters.
        planner_result = run_turtlebot3_recovery_planner(
            mission_ref=str(checkpoint.get("proposal_id") or ""),
            operator_instruction=json.dumps(
                {
                    "mission_instruction": proposal.get("operator_instruction"),
                    "requested_revision": dict(recovery_proposal),
                    "compiled_candidate": checkpoint_candidate(checkpoint),
                    "instruction": "Judge this proposed bounded action. Do not invent other parameters.",
                },
                ensure_ascii=False,
                default=str,
            ),
            battery_envelope=_mapping(proposal.get("battery_envelope")),
            home_distance_envelope=_mapping(proposal.get("home_distance_envelope")),
            autonomy_envelope=_mapping(proposal.get("autonomy_envelope")),
            obstacle_scenario=obstacle,
            runtime_motion_context=motion,
        )
        fresh_proposal = _mapping(planner_result.get("proposal"))
        if fresh_proposal.get("selected_action") == action:
            recovery_proposal = fresh_proposal
    graph_action = {"ask_human": "operator_review", "safe_stop": "hold"}.get(action, action)
    invocation = _mapping(planner_result.get("llm_invocation_evidence"))
    observed_inference = bool(
        planner_result.get("planner_status") == "proposal_guardrail_passed"
        and recovery_proposal.get("proposal_source") == "llm"
        and invocation.get("prompt_sha256")
        and invocation.get("response_sha256")
        and invocation.get("model_id")
        and str(invocation.get("provider") or "").startswith(("google_adk", "ollama_native"))
        and invocation.get("invocation_exit_code") == 0
    )
    resolution = _mapping(checkpoint.get("mission_incident_source_feasibility")) or _mapping(
        obstacle.get("recovery_candidate_resolution")
    )
    selected = _mapping(resolution.get("selected_candidate"))
    sequence = resolution.get("selected_sequence") or [selected]
    feasible = bool(
        resolution.get("resolution_status") == "validated"
        and resolution.get("dual_costmap_validated") is True
        and sequence
        and all(
            isinstance(item, Mapping)
            and item.get("path_valid") is True
            and item.get("core_action_feasibility_status") == "verified_feasible"
            and _mapping(item.get("core_action_feasibility")).get("artifact_id")
            for item in sequence
        )
    )
    candidate = checkpoint_candidate(checkpoint)
    result = {
        "schema_version": "missionos_nav2_recovery_graph_input.v1",
        "runtime_status": "proposal_guardrail_passed"
        if observed_inference
        else "guardrail_blocked",
        "blocking_reasons": [] if observed_inference else ["nav2_recovery_llm_inference_required"],
        "assessment": {
            "selected_bounded_action": graph_action,
            "proposed_parameters": _mapping(checkpoint.get("approved_parameters")),
            "compiled_candidate": candidate,
            "action_feasibility": {
                "action": graph_action,
                "feasibility_status": "verified_feasible" if feasible else "unverified",
                "nav2_candidate_resolution": resolution,
            },
        },
        "source_recovery_proposal": dict(recovery_proposal),
        "agent_invocations": [
            {
                **invocation,
                "agent_name": "missionos_turtlebot3_recovery_planner_agent",
                "agent_role": "recovery",
            }
        ]
        if observed_inference
        else [],
    }
    navigation_telemetry = _mapping(resolution.get("navigation_telemetry"))
    # Missing source time stays missing. The graph's own creation time cannot
    # establish freshness of a stored motion summary or a costmap observation.
    # A completed-segment timestamp also cannot date an absent planning snapshot.
    telemetry = navigation_telemetry or {
        "motion": dict(motion), "obstacle": dict(obstacle),
        "observed_at": None,
    }
    return run_missionos_mission_incident_graph(
        telemetry_snapshot=telemetry,
        mission_context={
            "task_id": checkpoint.get("proposal_id"),
            "mission_phase": "recovery_checkpoint",
            "execution_scope": "simulator",
            "mission_contract": mission_contract(proposal),
            "progress": {"completed_segment_count": checkpoint.get("completed_segment_count")},
            "observations": {
                "battery": proposal.get("battery_envelope"),
                "motion": dict(motion),
                "obstacle": dict(obstacle),
            },
            "constraints": {
                "assurance_policy": proposal.get("assurance_policy"),
                "autonomy_envelope": proposal.get("autonomy_envelope"),
                "compiled_candidate": candidate,
                "available_executor_actions": [action],
                "operator_approval_state": "not_approved",
            },
            "allowed_response_kinds": [
                "continue",
                "hold",
                "replan",
                "return",
                "operator_escalation",
            ],
        },
        recovery_policy=_mapping(proposal.get("autonomy_envelope")),
        recovery_runner=lambda **_: result,
        mission_assurance_agent=mission_assurance_agent,
        mission_assurance_timeout_seconds=120.0,
        navigation_backend="nav2",
    )


def turtlebot3_incident_dispatch_reasons(
    checkpoint: Mapping[str, Any], *,
    current_navigation_telemetry: Mapping[str, Any] | None = None,
    prediction_dispatch: bool = True,
    policy_sha256: str | None = None,
) -> list[str]:
    """Validate before approval/CAS and again before Nav2 execution."""
    graph = _mapping(checkpoint.get("missionos_mission_incident_graph"))
    if not graph:
        return ["mission_incident_graph_required_for_recovery_dispatch"]
    reasons = _frozen_graph_reasons(graph)
    recovery = _mapping(graph.get("recovery_result"))
    assessment = _mapping(recovery.get("assessment"))
    if assessment.get("compiled_candidate") != checkpoint_candidate(checkpoint):
        reasons.append("nav2_assurance_compiled_candidate_changed")
    if graph.get("recovery_proposed_action") != checkpoint.get("selected_action"):
        reasons.append("nav2_assurance_recovery_action_changed")
    if prediction_dispatch:
        from src.prediction.navigation import revalidate_navigation_prediction

        check = revalidate_navigation_prediction(
            _mapping(graph.get("navigation_prediction")),
            backend="nav2",
            telemetry=_mapping(current_navigation_telemetry),
            action=str(checkpoint.get("selected_action") or ""),
            parameters=_mapping(checkpoint.get("approved_parameters")),
            compiled_candidate=checkpoint_candidate(checkpoint),
            policy_sha256=policy_sha256,
        )
        if check.get("status") == "blocked":
            reasons.extend(check.get("reasons") or ["navigation_prediction_revalidation_blocked"])
    return list(dict.fromkeys(reasons))


def continue_turtlebot3_incident(*, checkpoint, approval, validate, execute,
                                 policy_authorization_handler=None):
    """Schedule real Nav2 continuation inside the same common authority graph."""
    result: dict[str, Any] = {}

    async def revalidate(_state):
        reasons = await asyncio.to_thread(validate)
        return {
            "validation_status": "blocked" if reasons else "valid",
            "proposal_id": checkpoint.get("checkpoint_id"),
            "reasons": reasons,
        }

    async def executor(_state):
        nonlocal result
        result = await asyncio.to_thread(execute, _state) if policy_authorization_handler else await asyncio.to_thread(execute)
        summary = _mapping(result.get("summary"))
        return {
            "executor_invoked": True,
            "dispatch_authority_created": summary.get(
                "recovery_execution_permitted_by_operator_approval"
            )
            is True or summary.get("recovery_execution_permitted_by_policy") is True,
            "dispatch_request_sent": summary.get("recovery_dispatch_request_sent") is True,
            "physical_execution_invoked": False,
        }

    def outcome():
        execution = _mapping(result.get("turtlebot3_home_mission_execution"))
        cycles = (
            execution.get("recovery_closed_loop_cycles")
            or _mapping(result.get("summary")).get("recovery_closed_loop_cycles")
            or []
        )
        return next(
            (
                _mapping(cycle.get("outcome_verification"))
                for cycle in cycles
                if cycle.get("checkpoint_id") == checkpoint.get("checkpoint_id")
            ),
            {},
        )

    def verifier(_state):
        observed = outcome()
        return {
            "verifier_status": observed.get("verification_status", "not_observed"),
            "command_ack_observed": observed.get("command_ack_observed") is True,
            "effect_observed": observed.get("executor_effect_observed") is True,
            "recovery_success_verified": observed.get("recovery_success_verified") is True,
            "outcome_verification": observed,
            "delivery_completion_claimed": False,
            "progress_counted": False,
        }

    def observe(_state):
        execution = _mapping(result.get("turtlebot3_home_mission_execution"))
        cycles = (
            execution.get("recovery_closed_loop_cycles")
            or _mapping(result.get("summary")).get("recovery_closed_loop_cycles")
            or []
        )
        cycle = next(
            (
                item
                for item in cycles
                if item.get("checkpoint_id") == checkpoint.get("checkpoint_id")
            ),
            {},
        )
        return {
            "next_mission_situation_created": bool(cycle.get("reobservation_sha256")),
            "behavior_delta": cycle.get("behavior_delta"),
            "outcome_verification": cycle.get("outcome_verification"),
            "reobservation_sha256": cycle.get("reobservation_sha256"),
            "route_resumed_after_recovery": _mapping(result.get("summary")).get(
                "route_resumed_after_recovery"
            ),
        }

    graph = run_missionos_mission_incident_continuation_graph(
        frozen_mission_incident_graph=_mapping(checkpoint.get("missionos_mission_incident_graph")),
        continuation_request={
            "task_id": checkpoint.get("proposal_id"),
            "proposal_id": checkpoint.get("checkpoint_id"),
            "recovery_action": checkpoint.get("selected_action"),
            "recovery_parameters": checkpoint.get("approved_parameters"),
            "explicit_recovery_dispatch_approval": approval.get(
                "explicit_recovery_dispatch_approval"
            )
            is True,
        },
        action_revalidation_handler=revalidate,
        executor_handler=executor,
        verifier_handler=verifier,
        observation_handler=observe,
        policy_authorization_handler=policy_authorization_handler,
    )
    if not result:
        result = {
            "summary": {
                "status": "blocked",
                "blocking_reasons": graph.get("blocking_reasons") or [],
                "dispatch_request_sent": False,
                "recovery_dispatch_request_sent": False,
                "completion_claimed": False,
                "physical_execution_invoked": False,
            }
        }
    result["missionos_mission_incident_continuation_graph"] = graph
    result["summary"]["missionos_mission_incident_continuation_graph"] = graph
    return result
