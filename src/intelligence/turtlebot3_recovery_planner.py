"""LLM-backed TurtleBot3 recovery proposal planner.

The planner may judge battery and home-distance context and propose a recovery
action. It does not approve, dispatch, count progress, or bypass the mission
autonomy envelope. When no LLM backend is configured, callers should keep their
deterministic fallback path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
import shlex
import subprocess
from typing import Any, Mapping, Sequence, cast
from urllib import request

from src.runtime.mission_autonomy_envelope import (
    MissionAutonomyRecoveryAction,
    build_mission_autonomy_recovery_proposal,
)
from src.runtime.missionos_llm_schemas import LLM_INVOCATION_EVIDENCE_SCHEMA_VERSION
from src.runtime.perception_claim import (
    CONSERVATIVE_RECOVERY_ACTIONS,
    PerceptionClaim,
    guard_perception_claim_support,
)


TURTLEBOT3_RECOVERY_PLANNER_RESULT_SCHEMA_VERSION = (
    "missionos_turtlebot3_recovery_planner_result.v1"
)
TURTLEBOT3_RECOVERY_PLANNER_GUARDRAIL_SCHEMA_VERSION = (
    "missionos_turtlebot3_recovery_planner_guardrail.v1"
)

TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV = "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_COMMAND"
TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV = (
    "MISSIONOS_ALLOW_TURTLEBOT3_RECOVERY_PLANNER_COMMAND_OVERRIDE"
)
TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED"
)
TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_ENV = (
    "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS"
)
TURTLEBOT3_RECOVERY_PLANNER_MODEL_ENV = "MISSIONOS_TURTLEBOT3_RECOVERY_PLANNER_MODEL_ID"
DEFAULT_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS = 60

_AGENT_NAME = "missionos_turtlebot3_recovery_planner_agent"
_ALLOWED_ACTIONS = {
    "continue",
    "return_home",
    "hold",
    "avoid_obstacle",
    "reroute",
    "safe_stop",
    "ask_human",
    "raw_velocity",
    "unbounded_move",
    "physical_execution",
    "payload_delivery_completion",
}
_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "approved",
        "approval_created",
        "approval_granted",
        "operator_approved",
        "dispatch_authority_created",
        "dispatch_request_sent",
        "gate_passed",
        "llm_judgment_in_gate",
        "progress_counted",
        "physical_execution_invoked",
        "completion_claimed",
        "mission_delivery_completion_claimed",
        "payload_delivery_completion_claimed",
    }
)
_ALLOWED_INPUT_OBSERVATION_KEYS = tuple(
    sorted(
        {
            "battery_start_pct",
            "battery_state_source",
            "completed_route_distance_m",
            "costmap_obstacle_observed",
            "dispatch_allowed",
            "distance_to_home_m",
            "distance_to_home_source",
            "distance_to_home_source_backed",
            "estimated_consumption_pct",
            "failed_segment_blocking_reason_count",
            "failed_segment_completion_claimed",
            "failed_segment_index",
            "failed_segment_label",
            "failed_recovery_action",
            "failed_recovery_blocking_reason_count",
            "failed_recovery_checkpoint_id",
            "failed_recovery_completion_claimed",
            "failed_recovery_result_count",
            "minimum_required_pct",
            "motion_observation_source",
            "motion_stall_threshold_m",
            "obstacle_challenge_requested",
            "odom_delta_m",
            "odom_topic",
            "planned_route_distance_m",
            "planned_segment_distance_m",
            "projected_return_battery_required_pct",
            "projected_return_reserve_pct",
            "recommended_avoidance_target_x_m",
            "recommended_avoidance_target_y_m",
            "recommended_recovery_action",
            "requires_new_human_approval",
            "reserve_pct",
            "robot_motion_observed",
            "route_progress_delta_m",
            "runtime_failure_observed",
            "runtime_failure_source",
            "runtime_obstacle_observed",
            "runtime_obstacle_recovery_required",
            "runtime_obstacle_source",
            "runtime_obstacle_x_m",
            "runtime_obstacle_y_m",
            "stalled_after_dispatch",
            "telemetry_window_ref",
        }
    )
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def build_turtlebot3_recovery_planner_prompt(
    *,
    mission_ref: str,
    operator_instruction: str,
    battery_envelope: Mapping[str, Any],
    home_distance_envelope: Mapping[str, Any],
    autonomy_envelope: Mapping[str, Any],
    obstacle_scenario: Mapping[str, Any] | None = None,
    indoor_delivery_route: Mapping[str, Any] | None = None,
    runtime_failure_context: Mapping[str, Any] | None = None,
    runtime_motion_context: Mapping[str, Any] | None = None,
    perception_claims: Sequence[PerceptionClaim | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the source-bound JSON prompt for Gemini/Ollama."""

    source_backed_input_observations = _source_observations_from_envelopes(
        battery_envelope=battery_envelope,
        home_distance_envelope=home_distance_envelope,
        obstacle_scenario=obstacle_scenario,
        runtime_failure_context=runtime_failure_context,
        runtime_motion_context=runtime_motion_context,
    )
    source_backed_input_observations = {
        key: value
        for key, value in source_backed_input_observations.items()
        if key in _ALLOWED_INPUT_OBSERVATION_KEYS
        and isinstance(value, (str, int, float, bool))
    }
    claim_payloads = [
        (
            claim
            if isinstance(claim, PerceptionClaim)
            else PerceptionClaim.model_validate(dict(claim))
        ).model_dump(mode="json")
        for claim in (perception_claims or ())
    ]
    return {
        "schema_version": "missionos_turtlebot3_recovery_planner_prompt.v1",
        "task": "propose_turtlebot3_recovery_action",
        "mission_ref": mission_ref,
        "operator_instruction": operator_instruction[:2000],
        "role_contract": {
            "llm_may_judge_and_propose": True,
            "llm_must_not_approve": True,
            "llm_must_not_dispatch": True,
            "rules_classify_execution_after_proposal": True,
            "proposal_first_classification": True,
        },
        "allowed_actions": sorted(_ALLOWED_ACTIONS),
        "preferred_recovery_actions": [
            "return_home",
            "avoid_obstacle",
            "hold",
            "ask_human",
        ],
        "battery_envelope": dict(battery_envelope),
        "home_distance_envelope": dict(home_distance_envelope),
        "autonomy_envelope": {
            "preapproved_recovery_actions": list(
                autonomy_envelope.get("preapproved_recovery_actions") or []
            ),
            "requires_human_approval_for": list(
                autonomy_envelope.get("requires_human_approval_for") or []
            ),
            "blocked_actions": list(autonomy_envelope.get("blocked_actions") or []),
        },
        "obstacle_scenario": dict(obstacle_scenario or {}),
        "indoor_delivery_route": dict(indoor_delivery_route or {}),
        "runtime_failure_context": dict(runtime_failure_context or {}),
        "runtime_motion_context": dict(runtime_motion_context or {}),
        "perception_claims": claim_payloads,
        "conservative_recovery_actions": sorted(CONSERVATIVE_RECOVERY_ACTIONS),
        "allowed_input_observation_keys": list(_ALLOWED_INPUT_OBSERVATION_KEYS),
        "source_backed_input_observations": source_backed_input_observations,
        "required_output_fields": [
            "selected_action",
            "reason",
            "input_observations",
        ],
        "strict_output_contract": (
            "Return exactly one JSON object. selected_action must be one of "
            "allowed_actions. Prefer return_home when battery is below the "
            "requested mission reserve and the source-backed home-distance "
            "envelope supports a bounded return. Prefer avoid_obstacle only "
            "when obstacle_scenario supplies source-backed runtime obstacle "
            "evidence. Prefer return_home or hold when runtime_failure_context "
            "supplies source-backed segment failure evidence. Use "
            "runtime_motion_context to distinguish no-motion stalls from "
            "near-complete progress. "
            "Use hold for critical stop/hold cases. You must not include approval, "
            "dispatch, gate, completion, progress, or physical execution fields. "
            "Every input_observations key and value must be copied exactly from "
            "source_backed_input_observations. Do not use an allowed key that is "
            "absent from source_backed_input_observations. input_observations must "
            "be a flat JSON object using only allowed_input_observation_keys; do "
            "not include "
            "battery_envelope, home_distance_envelope, obstacle_scenario, "
            "runtime_failure_context, runtime_motion_context, or any nested "
            "object as an input_observations key. "
            "Do not invent distance_to_home_m; copy it only from "
            "home_distance_envelope when you use it. "
            "If you rely on any perception_claims entry, list its claim_id in "
            "an optional cited_perception_claim_ids array. A claim whose "
            "corroborated_by is empty is camera-only evidence and may support "
            "only conservative_recovery_actions; selecting any other action "
            "based on an uncorroborated claim will be blocked."
        ),
    }


def _numeric_equal(left: Any, right: Any, *, tolerance: float = 1e-6) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    return abs(float(left) - float(right)) <= tolerance


def _observation_values_match(left: Any, right: Any) -> bool:
    if _numeric_equal(left, right):
        return True
    return left == right


def _observation_source_reasons(
    observations: Mapping[str, Any],
    *,
    source_observations: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    for key, value in sorted(observations.items(), key=lambda item: str(item[0])):
        if key not in source_observations:
            reasons.append(f"unsupported_observation_claim:{key}")
            continue
        if not _observation_values_match(value, source_observations.get(key)):
            reasons.append(f"observation_not_source_backed:{key}")
    return reasons


def _source_observations_from_envelopes(
    *,
    battery_envelope: Mapping[str, Any],
    home_distance_envelope: Mapping[str, Any],
    obstacle_scenario: Mapping[str, Any] | None = None,
    runtime_failure_context: Mapping[str, Any] | None = None,
    runtime_motion_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {}
    for key in (
        "battery_start_pct",
        "planned_route_distance_m",
        "estimated_consumption_pct",
        "reserve_pct",
        "minimum_required_pct",
    ):
        if isinstance(battery_envelope.get(key), (int, float)) and not isinstance(
            battery_envelope.get(key),
            bool,
        ):
            source[key] = battery_envelope[key]
    for key in ("battery_state_source", "dispatch_allowed"):
        if key in battery_envelope:
            source[key] = battery_envelope[key]
    for key in (
        "distance_to_home_m",
        "projected_return_battery_required_pct",
        "projected_return_reserve_pct",
    ):
        if isinstance(home_distance_envelope.get(key), (int, float)) and not isinstance(
            home_distance_envelope.get(key),
            bool,
        ):
            source[key] = home_distance_envelope[key]
    for key in ("distance_to_home_source", "distance_to_home_source_backed"):
        if key in home_distance_envelope:
            source[key] = home_distance_envelope[key]
    obstacle = obstacle_scenario if isinstance(obstacle_scenario, Mapping) else {}
    for key in (
        "runtime_obstacle_observed",
        "costmap_obstacle_observed",
        "obstacle_challenge_requested",
        "runtime_obstacle_recovery_required",
    ):
        if key in obstacle:
            source[key] = obstacle[key]
    for key in (
        "runtime_obstacle_x_m",
        "runtime_obstacle_y_m",
        "recommended_avoidance_target_x_m",
        "recommended_avoidance_target_y_m",
    ):
        if isinstance(obstacle.get(key), (int, float)) and not isinstance(
            obstacle.get(key),
            bool,
        ):
            source[key] = obstacle[key]
    for key in ("runtime_obstacle_source", "recommended_recovery_action"):
        if key in obstacle:
            source[key] = obstacle[key]
    failure = (
        runtime_failure_context
        if isinstance(runtime_failure_context, Mapping)
        else {}
    )
    for key in (
        "runtime_failure_observed",
        "failed_segment_completion_claimed",
        "failed_recovery_completion_claimed",
        "requires_new_human_approval",
    ):
        if key in failure:
            source[key] = failure[key]
    for key in (
        "failed_segment_index",
        "failed_segment_blocking_reason_count",
        "failed_recovery_blocking_reason_count",
        "failed_recovery_result_count",
    ):
        if isinstance(failure.get(key), (int, float)) and not isinstance(
            failure.get(key),
            bool,
        ):
            source[key] = failure[key]
    for key in (
        "failed_segment_label",
        "failed_recovery_checkpoint_id",
        "failed_recovery_action",
        "runtime_failure_source",
        "recommended_recovery_action",
    ):
        if key in failure:
            source[key] = failure[key]
    if isinstance(failure.get("failed_recovery_blocking_reasons"), list):
        source["failed_recovery_blocking_reasons"] = list(
            failure["failed_recovery_blocking_reasons"]
        )
    motion = (
        runtime_motion_context
        if isinstance(runtime_motion_context, Mapping)
        else {}
    )
    for key in (
        "robot_motion_observed",
        "stalled_after_dispatch",
    ):
        if key in motion:
            source[key] = motion[key]
    for key in (
        "odom_delta_m",
        "route_progress_delta_m",
        "completed_route_distance_m",
        "planned_segment_distance_m",
        "motion_stall_threshold_m",
    ):
        if isinstance(motion.get(key), (int, float)) and not isinstance(
            motion.get(key),
            bool,
        ):
            source[key] = motion[key]
    for key in (
        "motion_observation_source",
        "odom_topic",
        "telemetry_window_ref",
    ):
        if key in motion:
            source[key] = motion[key]
    return source


def guard_turtlebot3_recovery_planner_output(
    raw_output: Mapping[str, Any],
    *,
    source_observations: Mapping[str, Any] | None = None,
    perception_claims: Sequence[PerceptionClaim | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate output shape without replacing the LLM's recovery judgment."""

    blocking_reasons: list[str] = []
    checks = {
        "json_object": isinstance(raw_output, Mapping),
        "forbidden_authority_keys_absent": True,
        "selected_action_allowed": False,
        "reason_present": False,
        "input_observations_mapping": False,
        "input_observations_source_backed": False,
        "cited_perception_claim_ids_list": True,
        "cited_perception_claims_known": True,
        "perception_claim_support_respected": True,
    }
    for key in sorted(_FORBIDDEN_AUTHORITY_KEYS):
        if key in raw_output:
            checks["forbidden_authority_keys_absent"] = False
            blocking_reasons.append(f"raw_llm_output_forbidden_authority_key:{key}")
    selected_action = str(raw_output.get("selected_action") or "")
    checks["selected_action_allowed"] = selected_action in _ALLOWED_ACTIONS
    if not checks["selected_action_allowed"]:
        blocking_reasons.append("selected_action_not_allowed")
    reason = str(raw_output.get("reason") or "").strip()
    checks["reason_present"] = bool(reason)
    if not checks["reason_present"]:
        blocking_reasons.append("reason_required")
    observations = raw_output.get("input_observations")
    checks["input_observations_mapping"] = isinstance(observations, Mapping)
    if observations is not None and not checks["input_observations_mapping"]:
        blocking_reasons.append("input_observations_must_be_mapping")
    source_reasons = []
    if isinstance(observations, Mapping):
        source_reasons = _observation_source_reasons(
            observations,
            source_observations=dict(source_observations or {}),
        )
    checks["input_observations_source_backed"] = not source_reasons
    blocking_reasons.extend(source_reasons)

    provided_claims = list(perception_claims or ())
    raw_cited = raw_output.get("cited_perception_claim_ids")
    cited_claim_ids: list[str] = []
    if raw_cited is not None:
        if isinstance(raw_cited, (list, tuple)) and all(
            isinstance(item, str) for item in raw_cited
        ):
            cited_claim_ids = list(raw_cited)
        else:
            checks["cited_perception_claim_ids_list"] = False
            blocking_reasons.append(
                "cited_perception_claim_ids_must_be_string_list"
            )
    elif provided_claims:
        # Citation is the model's explicit reliance record. When claims were
        # in its context it must state what it relied on (an empty list is a
        # valid statement of non-reliance); omitting the field entirely is
        # not — review found omission could otherwise bypass the guard.
        checks["cited_perception_claim_ids_list"] = False
        blocking_reasons.append(
            "cited_perception_claim_ids_required_when_claims_present"
        )
    perception_support: dict[str, Any] = {}
    if checks["cited_perception_claim_ids_list"] and provided_claims:
        # The support rule runs whenever claims exist in context — never
        # gated on the model choosing to cite them.
        perception_support = guard_perception_claim_support(
            selected_action=selected_action,
            cited_claim_ids=cited_claim_ids,
            perception_claims=provided_claims,
        )
        checks.update(perception_support["checks"])
        blocking_reasons.extend(perception_support["blocking_reasons"])

    validated = {
        "selected_action": selected_action,
        "reason": reason,
        "input_observations": dict(observations or {})
        if isinstance(observations, Mapping)
        else {},
        "cited_perception_claim_ids": cited_claim_ids,
    }
    return {
        "schema_version": TURTLEBOT3_RECOVERY_PLANNER_GUARDRAIL_SCHEMA_VERSION,
        "guardrail_passed": not blocking_reasons,
        "checks": checks,
        "blocking_reasons": blocking_reasons,
        "validated_proposal": validated if not blocking_reasons else {},
        "perception_claim_support": perception_support,
        "progress_counted": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }


def run_turtlebot3_recovery_planner(
    *,
    mission_ref: str,
    operator_instruction: str,
    battery_envelope: Mapping[str, Any],
    home_distance_envelope: Mapping[str, Any],
    autonomy_envelope: Mapping[str, Any],
    obstacle_scenario: Mapping[str, Any] | None = None,
    indoor_delivery_route: Mapping[str, Any] | None = None,
    runtime_failure_context: Mapping[str, Any] | None = None,
    runtime_motion_context: Mapping[str, Any] | None = None,
    perception_claims: Sequence[PerceptionClaim | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the configured TurtleBot3 recovery planner."""

    prompt = build_turtlebot3_recovery_planner_prompt(
        mission_ref=mission_ref,
        operator_instruction=operator_instruction,
        battery_envelope=battery_envelope,
        home_distance_envelope=home_distance_envelope,
        autonomy_envelope=autonomy_envelope,
        obstacle_scenario=obstacle_scenario,
        indoor_delivery_route=indoor_delivery_route,
        runtime_failure_context=runtime_failure_context,
        runtime_motion_context=runtime_motion_context,
        perception_claims=perception_claims,
    )
    prompt_text = json.dumps(prompt, sort_keys=True)
    source_observations = _source_observations_from_envelopes(
        battery_envelope=battery_envelope,
        home_distance_envelope=home_distance_envelope,
        obstacle_scenario=obstacle_scenario,
        runtime_failure_context=runtime_failure_context,
        runtime_motion_context=runtime_motion_context,
    )
    if os.environ.get(TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV, "") == "1":
        return _run_adk_planner(
            prompt_text=prompt_text,
            mission_ref=mission_ref,
            source_observations=source_observations,
            perception_claims=perception_claims,
        )

    command_text = os.environ.get(TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV, "").strip()
    if command_text:
        if os.environ.get(TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV) != "1":
            return _planner_result(
                status="blocked",
                blocking_reasons=[
                    f"{TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV}_required"
                ],
            )
        return _run_command_override_planner(
            command_text=command_text,
            prompt_text=prompt_text,
            mission_ref=mission_ref,
            source_observations=source_observations,
            perception_claims=perception_claims,
        )

    return _planner_result(
        status="not_configured",
        blocking_reasons=[
            f"{TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV}_not_enabled",
            f"{TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV}_not_configured",
        ],
    )


def _run_adk_planner(
    *,
    prompt_text: str,
    mission_ref: str,
    source_observations: Mapping[str, Any],
    perception_claims: Sequence[PerceptionClaim | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    from src.agents.model_config import llm_provider_label

    started_at = datetime.now(timezone.utc)
    model_id = _planner_model_id()
    provider = llm_provider_label(_AGENT_NAME)
    invocation_kind = provider
    try:
        if provider == "google_adk_litellm_ollama":
            response_text = _invoke_ollama_response_text(
                prompt_text=prompt_text,
                model_id=model_id,
                timeout_seconds=_planner_timeout_seconds(),
            )
            provider = "ollama_native"
            invocation_kind = "ollama_chat_json"
        else:
            response_text = _invoke_adk_response_text(
                prompt_text=prompt_text,
                model_id=model_id,
                timeout_seconds=_planner_timeout_seconds(),
            )
    except Exception as exc:  # pragma: no cover - live backend failure shape varies.
        return _planner_result(
            status="blocked",
            blocking_reasons=[
                f"adk_recovery_planner_invocation_failed:{type(exc).__name__}"
            ],
        )
    completed_at = datetime.now(timezone.utc)
    raw_output = _read_json_object(response_text)
    if raw_output is None:
        return _planner_result(
            status="blocked",
            blocking_reasons=["adk_recovery_planner_response_not_json_object"],
            stdout_sha256=_sha256_text(response_text),
        )
    invocation_evidence = _invocation_evidence(
        provider=provider,
        invocation_kind=invocation_kind,
        model_id=model_id,
        prompt_text=prompt_text,
        response_text=response_text,
        started_at=started_at,
        completed_at=completed_at,
        exit_code=0,
        command_argv=None,
        process_pid=None,
        stderr_text="",
    )
    return _guard_and_build_proposal(
        raw_output=raw_output,
        mission_ref=mission_ref,
        invocation_evidence=invocation_evidence,
        source_observations=source_observations,
        perception_claims=perception_claims,
    )


def _run_command_override_planner(
    *,
    command_text: str,
    prompt_text: str,
    mission_ref: str,
    source_observations: Mapping[str, Any],
    perception_claims: Sequence[PerceptionClaim | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    command_argv = shlex.split(command_text)
    try:
        process = subprocess.Popen(
            command_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(
            input=prompt_text,
            timeout=_planner_timeout_seconds(),
        )
        exit_code = int(process.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _planner_result(
            status="blocked",
            blocking_reasons=[
                f"turtlebot3_recovery_planner_invocation_failed:{type(exc).__name__}"
            ],
        )
    completed_at = datetime.now(timezone.utc)
    if exit_code != 0:
        return _planner_result(
            status="blocked",
            blocking_reasons=["turtlebot3_recovery_planner_exit_code_nonzero"],
            invocation_exit_code=exit_code,
            stderr_sha256=_sha256_text(stderr),
        )
    raw_output = _read_json_object(stdout)
    if raw_output is None:
        return _planner_result(
            status="blocked",
            blocking_reasons=["turtlebot3_recovery_planner_stdout_not_json_object"],
            invocation_exit_code=exit_code,
            stdout_sha256=_sha256_text(stdout),
        )
    invocation_evidence = _invocation_evidence(
        provider="command_override",
        invocation_kind="subprocess",
        model_id=os.environ.get(
            TURTLEBOT3_RECOVERY_PLANNER_MODEL_ENV,
            "command_override_turtlebot3_recovery_planner",
        ),
        prompt_text=prompt_text,
        response_text=stdout,
        started_at=started_at,
        completed_at=completed_at,
        exit_code=exit_code,
        command_argv=command_argv,
        process_pid=int(process.pid),
        stderr_text=stderr,
    )
    return _guard_and_build_proposal(
        raw_output=raw_output,
        mission_ref=mission_ref,
        invocation_evidence=invocation_evidence,
        source_observations=source_observations,
        perception_claims=perception_claims,
    )


def _guard_and_build_proposal(
    *,
    raw_output: Mapping[str, Any],
    mission_ref: str,
    invocation_evidence: Mapping[str, Any],
    source_observations: Mapping[str, Any],
    perception_claims: Sequence[PerceptionClaim | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    guardrail = guard_turtlebot3_recovery_planner_output(
        raw_output,
        source_observations=source_observations,
        perception_claims=perception_claims,
    )
    if guardrail.get("guardrail_passed") is not True:
        return _planner_result(
            status="guardrail_blocked",
            blocking_reasons=list(guardrail.get("blocking_reasons") or []),
            guardrail=guardrail,
        )
    validated = guardrail["validated_proposal"]
    proposal = build_mission_autonomy_recovery_proposal(
        mission_ref=mission_ref,
        proposal_source="llm",
        selected_action=cast(
            MissionAutonomyRecoveryAction,
            validated["selected_action"],
        ),
        reason=validated["reason"],
        input_observations=validated["input_observations"],
        llm_invocation_evidence=invocation_evidence,
    )
    return _planner_result(
        status="proposal_guardrail_passed",
        proposal=proposal.model_dump(mode="json"),
        guardrail=guardrail,
        llm_invocation_evidence=invocation_evidence,
    )


def _invocation_evidence(
    *,
    provider: str,
    invocation_kind: str,
    model_id: str,
    prompt_text: str,
    response_text: str,
    started_at: datetime,
    completed_at: datetime,
    exit_code: int,
    command_argv: list[str] | None,
    process_pid: int | None,
    stderr_text: str,
) -> dict[str, Any]:
    evidence = {
        "schema_version": LLM_INVOCATION_EVIDENCE_SCHEMA_VERSION,
        "provider": provider,
        "model_id": model_id,
        "prompt_sha256": _sha256_text(prompt_text),
        "response_sha256": _sha256_text(response_text),
        "temperature": 0.0,
        "seed": None,
        "replay_n_runs": 1,
        "replay_agreement_ratio": 1.0,
        "llm_judgment_in_gate": False,
        "invocation_kind": invocation_kind,
        "process_pid": process_pid,
        "invocation_started_at": started_at.isoformat(),
        "invocation_completed_at": completed_at.isoformat(),
        "invocation_exit_code": exit_code,
        "invocation_stdout_sha256": _sha256_text(response_text),
        "invocation_stderr_sha256": _sha256_text(stderr_text),
    }
    if command_argv is not None:
        evidence["command_argv"] = list(command_argv)
    return evidence


def _planner_model_id() -> str:
    from src.agents.model_config import agent_model_label

    env_model = os.environ.get(TURTLEBOT3_RECOVERY_PLANNER_MODEL_ENV, "").strip()
    try:
        from src.config.settings import get_settings

        fallback = str(get_settings().agent_model)
    except Exception:
        fallback = "gemini-3.1-flash-lite"
    return agent_model_label(env_model or fallback, agent_name=_AGENT_NAME)


def _configure_google_adk_environment() -> None:
    from src.agents.model_config import (
        configure_google_vertex_location,
        google_llm_backend_enabled,
    )

    if not google_llm_backend_enabled(_AGENT_NAME):
        return
    try:
        from src.config.settings import get_settings

        settings = get_settings()
    except Exception:
        return
    api_key = str(getattr(settings, "google_api_key", "") or "").strip()
    if api_key and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = api_key
    if not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
        use_vertex = bool(getattr(settings, "google_genai_use_vertexai", False))
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true" if use_vertex else "false"
    configure_google_vertex_location(_planner_model_id(), agent_name=_AGENT_NAME)


def _invoke_adk_response_text(
    *,
    prompt_text: str,
    model_id: str,
    timeout_seconds: int,
) -> str:
    return asyncio.run(
        asyncio.wait_for(
            _invoke_adk_response_text_async(prompt_text=prompt_text, model_id=model_id),
            timeout=timeout_seconds,
        )
    )


async def _invoke_adk_response_text_async(*, prompt_text: str, model_id: str) -> str:
    _configure_google_adk_environment()
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.genai import types

    from src.agents.model_config import resolve_agent_model
    from src.runtime.session_service import create_session_service

    instruction = (
        "You are the MissionOS TurtleBot3 recovery planner. "
        "Return exactly one JSON object and no markdown. You may judge the "
        "battery/home-distance situation, runtime obstacle evidence, and runtime "
        "segment failure and motion-delta context, then propose selected_action, "
        "reason, and input_observations. input_observations must be a flat JSON "
        "object containing only scalar keys listed in the prompt's "
        "allowed_input_observation_keys. Copy keys and values exactly from the "
        "prompt's source_backed_input_observations and never use an allowed key "
        "that is absent there; never copy nested envelope/context objects into "
        "input_observations. You must not approve, dispatch, claim "
        "gate passage, claim completion, count progress, or claim physical "
        "execution."
    )
    agent = LlmAgent(
        name="missionos_turtlebot3_recovery_planner",
        model=resolve_agent_model(model_id, agent_name=_AGENT_NAME),
        instruction=instruction,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0,
            responseMimeType="application/json",
        ),
    )
    app_name = "missionos_turtlebot3_recovery_planner"
    user_id = "missionos_operator"
    session_service = create_session_service()
    session = await session_service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=session_service)
    content = types.Content(role="user", parts=[types.Part(text=prompt_text)])
    response_parts: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=content,
    ):
        if not event.is_final_response() or not event.content:
            continue
        for part in event.content.parts or []:
            text = getattr(part, "text", None)
            if text:
                response_parts.append(text)
    return "".join(response_parts).strip()


def _ollama_model_name(model_id: str) -> str:
    model = model_id.strip()
    prefix = "ollama_chat/"
    if model.startswith(prefix):
        return model[len(prefix) :]
    return model


def _ollama_base_url() -> str:
    agent_env_prefix = f"MISSIONOS_AGENT_{_AGENT_NAME.upper()}_"
    return (
        os.environ.get(f"{agent_env_prefix}OLLAMA_BASE_URL")
        or os.environ.get(f"{agent_env_prefix}LOCAL_API_BASE")
        or os.environ.get("MISSIONOS_OLLAMA_BASE_URL")
        or os.environ.get("MISSIONOS_LOCAL_API_BASE")
        or os.environ.get("BOILED_CLAW_OLLAMA_BASE_URL")
        or os.environ.get("BOILED_CLAW_LOCAL_API_BASE")
        or "http://localhost:11434"
    ).strip().rstrip("/")


def _invoke_ollama_response_text(
    *,
    prompt_text: str,
    model_id: str,
    timeout_seconds: int,
) -> str:
    instruction = (
        "You are the MissionOS TurtleBot3 recovery planner. Return exactly one "
        "JSON object and no markdown. You may judge the battery/home-distance "
        "situation, runtime obstacle evidence, and runtime segment failure "
        "and motion-delta context, then propose selected_action, reason, and "
        "input_observations. "
        "You must not approve, dispatch, claim gate passage, claim completion, "
        "count progress, or claim physical execution. Keep reason under 160 "
        "characters. input_observations must contain only these keys when "
        "present in the source envelopes: battery_start_pct, "
        "planned_route_distance_m, estimated_consumption_pct, "
        "minimum_required_pct, distance_to_home_m, distance_to_home_source, and "
        "projected_return_battery_required_pct, runtime_obstacle_observed, "
        "costmap_obstacle_observed, obstacle_challenge_requested, "
        "runtime_obstacle_recovery_required, runtime_obstacle_x_m, "
        "runtime_obstacle_y_m, recommended_avoidance_target_x_m, "
        "recommended_avoidance_target_y_m, runtime_obstacle_source, and "
        "recommended_recovery_action, runtime_failure_observed, "
        "failed_segment_index, failed_segment_label, runtime_failure_source, "
        "failed_segment_completion_claimed, failed_segment_blocking_reason_count, "
        "robot_motion_observed, odom_delta_m, route_progress_delta_m, "
        "completed_route_distance_m, planned_segment_distance_m, "
        "motion_observation_source, odom_topic, stalled_after_dispatch, "
        "motion_stall_threshold_m, telemetry_window_ref. Do not include nested "
        "battery_envelope, home_distance_envelope, obstacle_scenario, "
        "runtime_failure_context, or runtime_motion_context keys inside "
        "input_observations."
    )
    payload = {
        "model": _ollama_model_name(model_id),
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": prompt_text},
        ],
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"temperature": 0.0, "num_predict": 512},
    }
    http_request = request.Request(
        f"{_ollama_base_url()}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(http_request, timeout=timeout_seconds) as response:
        response_payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(response_payload, Mapping):
        return ""
    message = response_payload.get("message")
    if isinstance(message, Mapping) and isinstance(message.get("content"), str):
        return message["content"].strip()
    response_text = response_payload.get("response")
    return response_text.strip() if isinstance(response_text, str) else ""


def _planner_timeout_seconds() -> int:
    value = os.environ.get(TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_ENV)
    try:
        parsed = (
            int(value)
            if value is not None
            else DEFAULT_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS
        )
    except ValueError:
        return DEFAULT_TURTLEBOT3_RECOVERY_PLANNER_TIMEOUT_SECONDS
    return max(1, parsed)


def _planner_result(
    *,
    status: str,
    blocking_reasons: list[str] | None = None,
    proposal: Mapping[str, Any] | None = None,
    guardrail: Mapping[str, Any] | None = None,
    llm_invocation_evidence: Mapping[str, Any] | None = None,
    invocation_exit_code: int | None = None,
    stdout_sha256: str = "",
    stderr_sha256: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": TURTLEBOT3_RECOVERY_PLANNER_RESULT_SCHEMA_VERSION,
        "planner_status": status,
        "blocking_reasons": list(blocking_reasons or []),
        "proposal": dict(proposal or {}),
        "guardrail": dict(guardrail or {}),
        "llm_invocation_evidence": dict(llm_invocation_evidence or {}),
        "invocation_exit_code": invocation_exit_code,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


__all__ = [
    "TURTLEBOT3_RECOVERY_PLANNER_ADK_ENABLED_ENV",
    "TURTLEBOT3_RECOVERY_PLANNER_ALLOW_OVERRIDE_ENV",
    "TURTLEBOT3_RECOVERY_PLANNER_COMMAND_ENV",
    "TURTLEBOT3_RECOVERY_PLANNER_GUARDRAIL_SCHEMA_VERSION",
    "TURTLEBOT3_RECOVERY_PLANNER_RESULT_SCHEMA_VERSION",
    "build_turtlebot3_recovery_planner_prompt",
    "guard_turtlebot3_recovery_planner_output",
    "run_turtlebot3_recovery_planner",
]
