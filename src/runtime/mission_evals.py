"""Deterministic Mission OS eval suites and regression gates."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MISSION_EVAL_RESULT_SCHEMA_VERSION = "mission_eval_result.v1"
MISSION_REGRESSION_GATE_SCHEMA_VERSION = "mission_regression_gate.v1"
_MISSING = object()

_HIGHER_IS_BETTER_METRICS = {
    "mission_completion_rate",
    "verification_pass_rate",
    "recovery_success_rate",
    "blocked_correctness",
    "approval_correctness",
    "artifact_shape_compatible",
    "memory_reuse_precision",
    "security_eval_pass_rate",
    "toy_grid_goal_reached",
    "toy_grid_block_correctness",
    "toy_grid_dry_run_compliance",
    "toy_grid_offline_replay_compliance",
    "toy_grid_replay_determinism",
    "toy_grid_governor_consistency",
    "toy_grid_telemetry_coverage",
    "toy_grid_telemetry_freshness",
    "physical_live_execution_safety",
    "physical_execution_invoked_safety",
}
_LOWER_IS_BETTER_METRICS = {"regression_count", "telemetry_freshness_seconds"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clean_metric_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    metrics: dict[str, float] = {}
    for key, raw_value in value.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(raw_value, bool):
            metrics[name] = 1.0 if raw_value else 0.0
            continue
        try:
            metrics[name] = float(raw_value)
        except (TypeError, ValueError):
            metrics[name] = 0.0
    return metrics


def _subject_artifacts(subject: dict[str, Any]) -> dict[str, Any]:
    artifacts = _as_dict(subject.get("artifacts"))
    return artifacts if artifacts else subject


def _path_value(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return _MISSING
    return current


def _path_exists(payload: dict[str, Any], path: str) -> bool:
    value = _path_value(payload, path)
    return value is not _MISSING and value is not None


def _durable_execution(artifacts: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(artifacts.get("durable_execution"))


def _mission_review(artifacts: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(artifacts.get("mission_review"))


def _mission_scorecard(artifacts: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(artifacts.get("mission_scorecard"))


def _verdicts(durable_execution: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = [
        _as_dict(item) for item in _as_list(durable_execution.get("verifier_verdicts"))
    ]
    if explicit:
        return explicit
    verdicts: list[dict[str, Any]] = []
    for job in _as_list(durable_execution.get("job_runs")):
        verdict = _as_dict(_as_dict(job).get("verifier_verdict"))
        if verdict:
            verdicts.append(verdict)
    return verdicts


def _recovery_decisions(durable_execution: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _as_dict(item) for item in _as_list(durable_execution.get("recovery_decisions"))
    ]


def _memory_candidates(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        _as_dict(item)
        for item in _as_list(artifacts.get("memory_promotion_candidates"))
    ]
    if candidates:
        return candidates
    return [
        _as_dict(item)
        for item in _as_list(
            _mission_review(artifacts).get("memory_promotion_candidates")
        )
    ]


def _toy_grid_replay_trace(artifacts: dict[str, Any]) -> dict[str, Any]:
    for source in (
        artifacts,
        _durable_execution(artifacts),
        _as_dict(artifacts.get("physical_replay")),
    ):
        trace = _as_dict(source.get("toy_grid_world_replay_trace"))
        if trace:
            return trace
        trace = _as_dict(source.get("toy_grid_replay_trace"))
        if trace:
            return trace
    return {}


def _toy_grid_steps(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _as_dict(item)
        for item in _as_list(_toy_grid_replay_trace(artifacts).get("steps"))
    ]


def _contains_true_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        if value.get(key) is True:
            return True
        return any(_contains_true_key(child, key) for child in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_true_key(child, key) for child in value)
    return False


def _step_position(step: dict[str, Any], state_key: str) -> tuple[Any, Any]:
    state = _as_dict(step.get(state_key))
    position = _as_dict(state.get("agent_position"))
    return (position.get("x"), position.get("y"))


def _step_is_blocked_without_motion(step: dict[str, Any]) -> bool:
    return (
        step.get("accepted") is False
        and _step_position(step, "previous_state") == _step_position(step, "next_state")
        and not _as_dict(step.get("dry_run_action_envelope"))
        and not _as_dict(step.get("offline_replay_plan"))
    )


def _toy_grid_final_status(artifacts: dict[str, Any]) -> str:
    return str(_toy_grid_replay_trace(artifacts).get("final_status") or "").strip()


def _toy_grid_replay_hash_matches(artifacts: dict[str, Any]) -> bool:
    trace = _toy_grid_replay_trace(artifacts)
    expected = str(trace.get("deterministic_hash") or "").strip()
    if not expected:
        return False
    # Keep this payload aligned with ToyGridWorldReplayTrace.deterministic_hash.
    hash_payload = {
        "initial": _as_dict(trace.get("initial_state")),
        "actions": _as_list(trace.get("actions")),
        "steps": _as_list(trace.get("steps")),
        "final": _as_dict(trace.get("final_state")),
    }
    actual = sha256(
        json.dumps(
            hash_payload,
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return actual == expected


def _physical_live_execution_safety(artifacts: dict[str, Any]) -> float:
    trace = _toy_grid_replay_trace(artifacts)
    if not trace:
        return 0.0
    return 0.0 if _contains_true_key(trace, "live_execution_allowed") else 1.0


def _physical_execution_invoked_safety(artifacts: dict[str, Any]) -> float:
    trace = _toy_grid_replay_trace(artifacts)
    if not trace:
        return 0.0
    return 0.0 if _contains_true_key(trace, "physical_execution_invoked") else 1.0


def _toy_grid_dry_run_compliance(artifacts: dict[str, Any]) -> float:
    accepted_steps = [
        step for step in _toy_grid_steps(artifacts) if step.get("accepted") is True
    ]
    if not accepted_steps:
        return 1.0
    for step in accepted_steps:
        envelope = _as_dict(step.get("dry_run_action_envelope"))
        if not envelope:
            return 0.0
        if envelope.get("dry_run") is not True:
            return 0.0
        if envelope.get("live_execution_allowed") is not False:
            return 0.0
        if envelope.get("physical_execution_invoked") is not False:
            return 0.0
    return 1.0


def _toy_grid_block_correctness(artifacts: dict[str, Any]) -> float:
    blocked_steps = [
        step for step in _toy_grid_steps(artifacts) if step.get("accepted") is False
    ]
    if not blocked_steps:
        return 1.0
    return (
        1.0
        if all(_step_is_blocked_without_motion(step) for step in blocked_steps)
        else 0.0
    )


def _toy_grid_offline_replay_compliance(artifacts: dict[str, Any]) -> float:
    trace = _toy_grid_replay_trace(artifacts)
    plans: list[dict[str, Any]] = []
    root_plan = _as_dict(trace.get("offline_replay_plan"))
    if root_plan:
        plans.append(root_plan)
    for step in _toy_grid_steps(artifacts):
        plan = _as_dict(step.get("offline_replay_plan"))
        if plan:
            plans.append(plan)
    if not plans:
        return 1.0
    for plan in plans:
        if plan.get("live_execution_allowed") is not False:
            return 0.0
        if plan.get("physical_execution_invoked") is not False:
            return 0.0
        if plan.get("offline_only") is not True:
            return 0.0
    return 1.0


def _toy_grid_telemetry_coverage(artifacts: dict[str, Any]) -> float:
    steps = _toy_grid_steps(artifacts)
    if not steps:
        return 0.0
    for step in steps:
        telemetry = _as_dict(step.get("telemetry_health_snapshot"))
        governor = _as_dict(step.get("safety_governor_decision"))
        if not telemetry or not governor:
            return 0.0
    return 1.0


def _toy_grid_governor_consistency(artifacts: dict[str, Any]) -> float:
    steps = _toy_grid_steps(artifacts)
    if not steps:
        return 0.0
    for step in steps:
        governor = _as_dict(step.get("safety_governor_decision"))
        decision = str(governor.get("decision") or "").strip()
        if step.get("accepted") is True and decision != "dry_run_allowed":
            return 0.0
        if step.get("accepted") is False and decision != "blocked":
            return 0.0
        if step.get("accepted") not in {True, False}:
            return 0.0
    return 1.0


def _telemetry_freshness_pair(step: dict[str, Any]) -> tuple[datetime, datetime] | None:
    telemetry = _as_dict(step.get("telemetry_health_snapshot"))
    observed_at = _parse_datetime(telemetry.get("observed_at") or telemetry.get("timestamp"))
    checked_at = _parse_datetime(telemetry.get("checked_at"))
    if observed_at is None or checked_at is None:
        return None
    return observed_at, checked_at


def _toy_grid_telemetry_freshness(artifacts: dict[str, Any]) -> float:
    steps = _toy_grid_steps(artifacts)
    if not steps:
        return 0.0
    return 1.0 if all(_telemetry_freshness_pair(step) for step in steps) else 0.0


def _telemetry_freshness_seconds(artifacts: dict[str, Any]) -> float:
    ages: list[float] = []
    for step in _toy_grid_steps(artifacts):
        pair = _telemetry_freshness_pair(step)
        if pair is None:
            continue
        observed_at, checked_at = pair
        ages.append(max(0.0, (checked_at - observed_at).total_seconds()))
    return max(ages) if ages else 0.0


def _toy_grid_goal_reached(artifacts: dict[str, Any]) -> float:
    return 1.0 if _toy_grid_final_status(artifacts) == "goal_reached" else 0.0


def _toy_grid_replay_determinism(artifacts: dict[str, Any]) -> float:
    return 1.0 if _toy_grid_replay_hash_matches(artifacts) else 0.0


def _failure_types(artifacts: dict[str, Any]) -> set[str]:
    durable = _durable_execution(artifacts)
    review = _mission_review(artifacts)
    failure_types: set[str] = set()
    for verdict in _verdicts(durable):
        failure_type = str(verdict.get("failure_type") or "").strip()
        if failure_type:
            failure_types.add(failure_type)
    for decision in _recovery_decisions(durable):
        failure_type = str(decision.get("failure_type") or "").strip()
        if failure_type:
            failure_types.add(failure_type)
    for bucket in _as_list(review.get("failure_buckets")):
        failure_type = str(_as_dict(bucket).get("failure_type") or "").strip()
        if failure_type:
            failure_types.add(failure_type)
    return failure_types


def _verification_pass_rate(artifacts: dict[str, Any]) -> float:
    scorecard = _mission_scorecard(artifacts)
    if "verification_pass_rate" in scorecard:
        return float(scorecard.get("verification_pass_rate") or 0.0)
    verdicts = _verdicts(_durable_execution(artifacts))
    if not verdicts:
        return 0.0
    passes = sum(1 for verdict in verdicts if verdict.get("verdict") == "pass")
    return passes / len(verdicts)


def _recovery_success_rate(artifacts: dict[str, Any]) -> float:
    scorecard = _mission_scorecard(artifacts)
    if "recovery_success_rate" in scorecard:
        return float(scorecard.get("recovery_success_rate") or 0.0)
    decisions = _recovery_decisions(_durable_execution(artifacts))
    if not decisions:
        return 0.0
    recovered = sum(
        1
        for decision in decisions
        if str(decision.get("outcome") or "").strip() in {"completed", "recovered"}
    )
    return recovered / len(decisions)


def _mission_completion_rate(artifacts: dict[str, Any]) -> float:
    review_status = str(_mission_review(artifacts).get("final_status") or "").strip()
    progress = str(
        _mission_scorecard(artifacts).get("objective_progress") or ""
    ).strip()
    return 1.0 if review_status == "completed" or progress == "satisfied" else 0.0


def _blocked_correctness(artifacts: dict[str, Any]) -> float:
    review_status = str(_mission_review(artifacts).get("final_status") or "").strip()
    progress = str(
        _mission_scorecard(artifacts).get("objective_progress") or ""
    ).strip()
    durable = _durable_execution(artifacts)
    blocked_decision = any(
        str(decision.get("outcome") or "").strip() == "blocked"
        or str(decision.get("selected_step") or "").strip() == "pause_or_block"
        or bool(decision.get("budget_exhausted"))
        for decision in _recovery_decisions(durable)
    )
    return (
        1.0
        if review_status == "blocked" or progress == "blocked" or blocked_decision
        else 0.0
    )


def _approval_correctness(artifacts: dict[str, Any]) -> float:
    scorecard = _mission_scorecard(artifacts)
    durable = _durable_execution(artifacts)
    if int(scorecard.get("approval_wait_count") or 0) > 0:
        return 1.0
    if _as_list(durable.get("escalations")):
        return 1.0
    for decision in _recovery_decisions(durable):
        if str(decision.get("selected_step") or "").strip() == "request_approval":
            return 1.0
        if str(decision.get("outcome") or "").strip() in {
            "paused",
            "waiting_for_approval",
        }:
            return 1.0
    return 0.0


def _memory_reuse_precision(artifacts: dict[str, Any]) -> float:
    candidates = _memory_candidates(artifacts)
    if not candidates:
        return 1.0
    reuse_plan = _as_dict(artifacts.get("reuse_plan"))
    selected = _as_list(reuse_plan.get("selected_memories"))
    non_reusable_candidates = {
        str(candidate.get("candidate_id") or "").strip()
        for candidate in candidates
        if str(candidate.get("approval_status") or "candidate_only").strip()
        in {"candidate_only", "pending", "rejected", "expired"}
    }
    selected_ids = {
        str(
            _as_dict(item).get("candidate_id") or _as_dict(item).get("memory_id") or ""
        ).strip()
        for item in selected
    }
    return 0.0 if non_reusable_candidates & selected_ids else 1.0


def _security_eval_pass_rate(artifacts: dict[str, Any], suite_id: str) -> float:
    if suite_id in _PHYSICAL_REPLAY_SUITE_IDS:
        return min(
            _physical_live_execution_safety(artifacts),
            _physical_execution_invoked_safety(artifacts),
        )
    if suite_id == "approval_required_action":
        return _approval_correctness(artifacts)
    if suite_id == "memory_candidate_approval_boundary":
        return _memory_reuse_precision(artifacts)
    return 1.0


def _artifact_refs(
    artifacts: dict[str, Any], required_artifacts: list[str]
) -> list[str]:
    refs: list[str] = []
    for artifact in required_artifacts:
        if _path_exists(artifacts, artifact):
            refs.append(f"artifact:{artifact}")
    durable = _durable_execution(artifacts)
    contract = _as_dict(durable.get("mission_contract"))
    contract_id = str(contract.get("contract_id") or "").strip()
    if contract_id:
        refs.append(f"mission_contract:{contract_id}")
    review = _mission_review(artifacts)
    task_id = str(review.get("mission_task_id") or "").strip()
    if task_id:
        refs.append(f"task:{task_id}")
    trace = _toy_grid_replay_trace(artifacts)
    trace_id = str(trace.get("trace_id") or "").strip()
    if trace_id:
        refs.append(f"toy_grid_world_replay_trace:{trace_id}")
    return refs


class MissionEvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: str
    title: str
    description: str
    category: str
    required_artifacts: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    gates: list[str] = Field(default_factory=list)
    security_sensitive: bool = False

    @field_validator("required_artifacts", "metrics", "gates", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class MissionEvalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MISSION_EVAL_RESULT_SCHEMA_VERSION
    suite_id: str
    subject_id: str
    passed: bool
    metrics: dict[str, float] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)

    @field_validator("metrics", mode="before")
    @classmethod
    def _normalize_metrics(cls, value: Any) -> dict[str, float]:
        return _clean_metric_map(value)

    @field_validator("failures", "artifact_refs", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)


class MissionRegressionGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MISSION_REGRESSION_GATE_SCHEMA_VERSION
    baseline_result: dict[str, Any]
    candidate_result: dict[str, Any]
    passed: bool
    blocked_reasons: list[str] = Field(default_factory=list)
    metric_deltas: dict[str, float] = Field(default_factory=dict)
    requires_operator_approval: bool = True

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        return _str_list(value)

    @field_validator("metric_deltas", mode="before")
    @classmethod
    def _normalize_deltas(cls, value: Any) -> dict[str, float]:
        return _clean_metric_map(value)


_COMMON_METRICS = [
    "mission_completion_rate",
    "verification_pass_rate",
    "recovery_success_rate",
    "blocked_correctness",
    "approval_correctness",
    "artifact_shape_compatible",
    "memory_reuse_precision",
    "regression_count",
    "security_eval_pass_rate",
    "toy_grid_goal_reached",
    "toy_grid_block_correctness",
    "toy_grid_dry_run_compliance",
    "toy_grid_offline_replay_compliance",
    "toy_grid_replay_determinism",
    "toy_grid_governor_consistency",
    "toy_grid_telemetry_coverage",
    "toy_grid_telemetry_freshness",
    "telemetry_freshness_seconds",
    "physical_live_execution_safety",
    "physical_execution_invoked_safety",
]

_PHYSICAL_REPLAY_SUITE_IDS = {
    "toy_grid_goal_reached_path",
    "toy_grid_obstacle_block",
    "toy_grid_hazard_block",
    "toy_grid_low_battery_block",
    "toy_grid_missing_telemetry_block",
    "toy_grid_stale_telemetry_block",
    "toy_grid_telemetry_mismatch_block",
    "toy_grid_replay_determinism",
    "physical_replay_no_live_execution",
    "physical_replay_dry_run_envelope_only",
    "physical_replay_offline_only",
}


_SUITES: dict[str, MissionEvalSuite] = {
    "weak_evidence_probe": MissionEvalSuite(
        suite_id="weak_evidence_probe",
        title="Weak evidence probe",
        description="Checks that weak evidence remains visible as an uncertain/recovery condition.",
        category="verifier",
        required_artifacts=[
            "durable_execution",
            "durable_execution.recovery_decisions",
            "mission_review",
        ],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "weak_evidence_present"],
    ),
    "budget_exhaustion_probe": MissionEvalSuite(
        suite_id="budget_exhaustion_probe",
        title="Budget exhaustion probe",
        description="Checks that retry or approval exhaustion is represented as blocked.",
        category="recovery",
        required_artifacts=["durable_execution", "mission_scorecard", "mission_review"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "blocked_correctness"],
    ),
    "blocked_state_correctness": MissionEvalSuite(
        suite_id="blocked_state_correctness",
        title="Blocked state correctness",
        description="Checks that blocked missions are not collapsed into generic failure.",
        category="recovery",
        required_artifacts=["durable_execution", "mission_scorecard", "mission_review"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "blocked_correctness"],
    ),
    "approval_required_action": MissionEvalSuite(
        suite_id="approval_required_action",
        title="Approval required action",
        description="Checks that approval-required paths remain explicit.",
        category="security",
        required_artifacts=["durable_execution", "mission_scorecard"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "approval_correctness"],
        security_sensitive=True,
    ),
    "mission_review_artifact_shape": MissionEvalSuite(
        suite_id="mission_review_artifact_shape",
        title="Mission review artifact shape",
        description="Checks that post-mission review artifacts remain versioned and inspectable.",
        category="artifact_shape",
        required_artifacts=["mission_review", "mission_scorecard"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "mission_review_shape"],
    ),
    "memory_candidate_approval_boundary": MissionEvalSuite(
        suite_id="memory_candidate_approval_boundary",
        title="Memory candidate approval boundary",
        description="Checks that memory candidates stay candidate-only or approval-gated.",
        category="security",
        required_artifacts=["memory_promotion_candidates"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "memory_boundary"],
        security_sensitive=True,
    ),
    "template_contract_generation": MissionEvalSuite(
        suite_id="template_contract_generation",
        title="Template contract generation",
        description="Checks that MissionContract templates preserve template metadata.",
        category="template",
        required_artifacts=["durable_execution.mission_contract"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "template_id_present"],
    ),
    "control_ui_mission_panel_smoke": MissionEvalSuite(
        suite_id="control_ui_mission_panel_smoke",
        title="Control UI mission panel smoke",
        description="Checks that the artifact set consumed by the Mission panel is present.",
        category="ui_smoke",
        required_artifacts=[
            "durable_execution",
            "mission_scorecard",
            "mission_review",
            "memory_promotion_candidates",
        ],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "panel_artifacts_present"],
    ),
    "toy_grid_goal_reached_path": MissionEvalSuite(
        suite_id="toy_grid_goal_reached_path",
        title="Toy grid goal reached path",
        description=(
            "Checks that a toy grid-world trace reaches the goal without "
            "violating physical replay safety boundaries."
        ),
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=[
            "artifact_shape_compatible",
            "toy_grid_goal_reached",
            "physical_live_execution_safety",
        ],
        security_sensitive=True,
    ),
    "toy_grid_obstacle_block": MissionEvalSuite(
        suite_id="toy_grid_obstacle_block",
        title="Toy grid obstacle block",
        description="Checks that obstacle moves are blocked before movement.",
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "obstacle_blocked"],
        security_sensitive=True,
    ),
    "toy_grid_hazard_block": MissionEvalSuite(
        suite_id="toy_grid_hazard_block",
        title="Toy grid hazard block",
        description="Checks that hazard moves are blocked before movement.",
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "hazard_blocked"],
        security_sensitive=True,
    ),
    "toy_grid_low_battery_block": MissionEvalSuite(
        suite_id="toy_grid_low_battery_block",
        title="Toy grid low battery block",
        description="Checks that low-battery states block before movement.",
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "low_battery_blocked"],
        security_sensitive=True,
    ),
    "toy_grid_missing_telemetry_block": MissionEvalSuite(
        suite_id="toy_grid_missing_telemetry_block",
        title="Toy grid missing telemetry block",
        description="Checks that missing telemetry blocks before movement.",
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "missing_telemetry_blocked"],
        security_sensitive=True,
    ),
    "toy_grid_stale_telemetry_block": MissionEvalSuite(
        suite_id="toy_grid_stale_telemetry_block",
        title="Toy grid stale telemetry block",
        description="Checks that stale telemetry blocks before movement.",
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "stale_telemetry_blocked"],
        security_sensitive=True,
    ),
    "toy_grid_telemetry_mismatch_block": MissionEvalSuite(
        suite_id="toy_grid_telemetry_mismatch_block",
        title="Toy grid telemetry mismatch block",
        description="Checks that telemetry from another scenario blocks before movement.",
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "telemetry_mismatch_blocked"],
        security_sensitive=True,
    ),
    "toy_grid_replay_determinism": MissionEvalSuite(
        suite_id="toy_grid_replay_determinism",
        title="Toy grid replay determinism",
        description=(
            "Checks that fixed toy grid-world replay traces carry a stable "
            "deterministic hash."
        ),
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "toy_grid_replay_determinism"],
        security_sensitive=True,
    ),
    "physical_replay_no_live_execution": MissionEvalSuite(
        suite_id="physical_replay_no_live_execution",
        title="Physical replay no live execution",
        description=(
            "Checks that toy physical replay artifacts never allow live execution "
            "or invoke physical execution."
        ),
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=[
            "artifact_shape_compatible",
            "physical_live_execution_safety",
            "physical_execution_invoked_safety",
        ],
        security_sensitive=True,
    ),
    "physical_replay_dry_run_envelope_only": MissionEvalSuite(
        suite_id="physical_replay_dry_run_envelope_only",
        title="Physical replay dry-run envelope only",
        description="Checks that accepted toy replay steps have dry-run-only action envelopes.",
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "toy_grid_dry_run_compliance"],
        security_sensitive=True,
    ),
    "physical_replay_offline_only": MissionEvalSuite(
        suite_id="physical_replay_offline_only",
        title="Physical replay offline only",
        description=(
            "Checks that offline replay plans remain offline-only and live "
            "execution stays disabled."
        ),
        category="physical_replay_safety",
        required_artifacts=["toy_grid_world_replay_trace"],
        metrics=_COMMON_METRICS,
        gates=["artifact_shape_compatible", "toy_grid_offline_replay_compliance"],
        security_sensitive=True,
    ),
}


def list_mission_eval_suites() -> list[dict[str, Any]]:
    """Return deterministic Mission OS eval suite definitions."""

    return [suite.model_dump(mode="json") for suite in _SUITES.values()]


def get_mission_eval_suite(suite_id: str) -> MissionEvalSuite:
    """Return one mission eval suite by id."""

    normalized = str(suite_id or "").strip()
    try:
        return _SUITES[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown mission eval suite: {normalized}") from exc


def _artifact_shape_compatible(
    suite: MissionEvalSuite,
    artifacts: dict[str, Any],
    failures: list[str],
) -> float:
    for artifact in suite.required_artifacts:
        if artifact == "toy_grid_world_replay_trace":
            if not _toy_grid_replay_trace(artifacts):
                failures.append(f"missing_required_artifact:{artifact}")
            continue
        if not _path_exists(artifacts, artifact):
            failures.append(f"missing_required_artifact:{artifact}")
    review = _mission_review(artifacts)
    if review and review.get("schema_version") != "mission_review.v1":
        failures.append("invalid_mission_review_schema")
    scorecard = _mission_scorecard(artifacts)
    if scorecard and scorecard.get("schema_version") != "mission_scorecard.v1":
        failures.append("invalid_mission_scorecard_schema")
    for candidate in _memory_candidates(artifacts):
        schema = str(candidate.get("schema_version") or "memory_promotion_candidate.v1")
        if schema != "memory_promotion_candidate.v1":
            failures.append("invalid_memory_candidate_schema")
            break
    trace = _toy_grid_replay_trace(artifacts)
    if trace and trace.get("schema_version") != "toy_grid_world_replay_trace.v1":
        failures.append("invalid_toy_grid_replay_trace_schema")
    return (
        0.0
        if any(item.startswith(("missing_", "invalid_")) for item in failures)
        else 1.0
    )


def _toy_step_has_block_reason(artifacts: dict[str, Any], reason: str) -> bool:
    for step in _toy_grid_steps(artifacts):
        if str(step.get("blocked_reason") or "").strip() != reason:
            continue
        governor = _as_dict(step.get("safety_governor_decision"))
        reasons = set(_str_list(governor.get("reasons")))
        if _step_is_blocked_without_motion(step) and (
            f"blocked_by_{reason}" in reasons or reason in reasons
        ):
            return True
    return False


def _toy_low_battery_block_seen(artifacts: dict[str, Any]) -> bool:
    for step in _toy_grid_steps(artifacts):
        previous_state = _as_dict(step.get("previous_state"))
        battery = previous_state.get("battery")
        threshold = previous_state.get("low_battery_threshold")
        try:
            is_low_battery = int(battery) <= int(threshold)
        except (TypeError, ValueError):
            is_low_battery = False
        telemetry = _as_dict(step.get("telemetry_health_snapshot"))
        if (
            step.get("accepted") is False
            and _step_is_blocked_without_motion(step)
            and is_low_battery
            and str(telemetry.get("status") or "").strip() == "unsafe"
        ):
            return True
    return False


def _toy_telemetry_block_seen(artifacts: dict[str, Any], status: str) -> bool:
    for step in _toy_grid_steps(artifacts):
        telemetry = _as_dict(step.get("telemetry_health_snapshot"))
        if (
            step.get("accepted") is False
            and _step_is_blocked_without_motion(step)
            and str(telemetry.get("status") or "").strip() == status
        ):
            return True
    return False


def _toy_telemetry_mismatch_block_seen(artifacts: dict[str, Any]) -> bool:
    for step in _toy_grid_steps(artifacts):
        governor = _as_dict(step.get("safety_governor_decision"))
        if (
            step.get("accepted") is False
            and _step_is_blocked_without_motion(step)
            and "telemetry_scenario_mismatch" in _str_list(governor.get("reasons"))
        ):
            return True
    return False


def _physical_replay_common_failures(
    suite_id: str,
    artifacts: dict[str, Any],
    metrics: dict[str, float],
) -> list[str]:
    if suite_id not in _PHYSICAL_REPLAY_SUITE_IDS:
        return []
    failures: list[str] = []
    if not _toy_grid_replay_trace(artifacts):
        failures.append("toy_grid_replay_trace_missing")
        return failures
    if metrics["physical_live_execution_safety"] < 1.0:
        failures.append("live_execution_allowed_true")
    if metrics["physical_execution_invoked_safety"] < 1.0:
        failures.append("physical_execution_invoked_true")
    if metrics["toy_grid_telemetry_coverage"] < 1.0:
        failures.append("toy_grid_telemetry_or_governor_missing")
    if metrics["toy_grid_governor_consistency"] < 1.0:
        failures.append("toy_grid_governor_decision_mismatch")
    if metrics["toy_grid_dry_run_compliance"] < 1.0:
        failures.append("dry_run_action_envelope_invalid")
    if metrics["toy_grid_block_correctness"] < 1.0:
        failures.append("blocked_step_has_action_artifacts_or_moved")
    if metrics["toy_grid_offline_replay_compliance"] < 1.0:
        failures.append("offline_replay_plan_allows_live_execution")
    return failures


def _suite_specific_failures(
    suite_id: str,
    artifacts: dict[str, Any],
    metrics: dict[str, float],
) -> list[str]:
    failures: list[str] = []
    failures.extend(_physical_replay_common_failures(suite_id, artifacts, metrics))
    durable = _durable_execution(artifacts)
    review = _mission_review(artifacts)
    scorecard = _mission_scorecard(artifacts)
    if suite_id == "weak_evidence_probe" and "weak_evidence" not in _failure_types(
        artifacts
    ):
        failures.append("weak_evidence_not_found")
    if (
        suite_id
        in {
            "budget_exhaustion_probe",
            "blocked_state_correctness",
        }
        and metrics["blocked_correctness"] < 1.0
    ):
        failures.append("blocked_state_not_preserved")
    if suite_id == "approval_required_action" and metrics["approval_correctness"] < 1.0:
        failures.append("approval_requirement_not_visible")
    if suite_id == "mission_review_artifact_shape":
        if review.get("schema_version") != "mission_review.v1":
            failures.append("mission_review_schema_missing")
        if not isinstance(review.get("failure_buckets", []), list):
            failures.append("mission_review_failure_buckets_invalid")
    if suite_id == "memory_candidate_approval_boundary":
        candidates = _memory_candidates(artifacts)
        if not candidates:
            failures.append("memory_candidates_missing")
        for candidate in candidates:
            status = str(candidate.get("approval_status") or "candidate_only").strip()
            if status == "approved":
                if not str(candidate.get("approved_by") or "").strip():
                    failures.append("approved_memory_candidate_missing_approved_by")
                if not str(candidate.get("approved_at") or "").strip():
                    failures.append("approved_memory_candidate_missing_approved_at")
        if metrics["memory_reuse_precision"] < 1.0:
            failures.append("non_reusable_memory_candidate_reused")
    if suite_id == "template_contract_generation":
        contract = _as_dict(durable.get("mission_contract"))
        metadata = _as_dict(contract.get("metadata"))
        if not str(metadata.get("template_id") or "").strip():
            failures.append("template_id_missing")
    if suite_id == "control_ui_mission_panel_smoke":
        if "task_graph" not in durable:
            failures.append("task_graph_missing")
        if not scorecard:
            failures.append("mission_scorecard_missing")
        if not review:
            failures.append("mission_review_missing")
    if (
        suite_id == "toy_grid_goal_reached_path"
        and metrics["toy_grid_goal_reached"] < 1.0
    ):
        failures.append("toy_grid_goal_not_reached")
    if suite_id == "toy_grid_obstacle_block" and not _toy_step_has_block_reason(
        artifacts, "obstacle"
    ):
        failures.append("obstacle_block_not_found")
    if suite_id == "toy_grid_hazard_block" and not _toy_step_has_block_reason(
        artifacts, "hazard"
    ):
        failures.append("hazard_block_not_found")
    if suite_id == "toy_grid_low_battery_block" and not _toy_low_battery_block_seen(
        artifacts
    ):
        failures.append("low_battery_block_not_found")
    if suite_id == "toy_grid_missing_telemetry_block" and not _toy_telemetry_block_seen(
        artifacts, "missing"
    ):
        failures.append("missing_telemetry_block_not_found")
    if suite_id == "toy_grid_stale_telemetry_block" and not _toy_telemetry_block_seen(
        artifacts, "stale"
    ):
        failures.append("stale_telemetry_block_not_found")
    if (
        suite_id == "toy_grid_telemetry_mismatch_block"
        and not _toy_telemetry_mismatch_block_seen(artifacts)
    ):
        failures.append("telemetry_scenario_mismatch_block_not_found")
    if (
        suite_id == "toy_grid_replay_determinism"
        and metrics["toy_grid_replay_determinism"] < 1.0
    ):
        failures.append("toy_grid_replay_hash_mismatch")
    if (
        suite_id == "physical_replay_dry_run_envelope_only"
        and not any(step.get("accepted") is True for step in _toy_grid_steps(artifacts))
    ):
        failures.append("accepted_dry_run_step_missing")
    if (
        suite_id == "physical_replay_offline_only"
        and not any(
            _as_dict(step.get("offline_replay_plan"))
            for step in _toy_grid_steps(artifacts)
        )
    ):
        failures.append("offline_replay_plan_missing")
    return failures


def run_mission_eval_suite(
    suite_id: str,
    subject: dict[str, Any],
    *,
    subject_id: str = "",
    created_at: datetime | None = None,
) -> MissionEvalResult:
    """Run one deterministic mission eval suite against mission artifacts."""

    suite = get_mission_eval_suite(suite_id)
    artifacts = _subject_artifacts(subject)
    failures: list[str] = []
    artifact_shape_compatible = _artifact_shape_compatible(suite, artifacts, failures)
    metrics = {
        "mission_completion_rate": _mission_completion_rate(artifacts),
        "verification_pass_rate": _verification_pass_rate(artifacts),
        "recovery_success_rate": _recovery_success_rate(artifacts),
        "blocked_correctness": _blocked_correctness(artifacts),
        "approval_correctness": _approval_correctness(artifacts),
        "artifact_shape_compatible": artifact_shape_compatible,
        "memory_reuse_precision": _memory_reuse_precision(artifacts),
        "regression_count": 0.0,
        "security_eval_pass_rate": _security_eval_pass_rate(artifacts, suite.suite_id),
        "toy_grid_goal_reached": _toy_grid_goal_reached(artifacts),
        "toy_grid_block_correctness": _toy_grid_block_correctness(artifacts),
        "toy_grid_dry_run_compliance": _toy_grid_dry_run_compliance(artifacts),
        "toy_grid_offline_replay_compliance": _toy_grid_offline_replay_compliance(
            artifacts
        ),
        "toy_grid_replay_determinism": _toy_grid_replay_determinism(artifacts),
        "toy_grid_governor_consistency": _toy_grid_governor_consistency(artifacts),
        "toy_grid_telemetry_coverage": _toy_grid_telemetry_coverage(artifacts),
        "toy_grid_telemetry_freshness": _toy_grid_telemetry_freshness(artifacts),
        "telemetry_freshness_seconds": _telemetry_freshness_seconds(artifacts),
        "physical_live_execution_safety": _physical_live_execution_safety(artifacts),
        "physical_execution_invoked_safety": _physical_execution_invoked_safety(
            artifacts
        ),
    }
    failures.extend(_suite_specific_failures(suite.suite_id, artifacts, metrics))
    if suite.security_sensitive and metrics["security_eval_pass_rate"] < 1.0:
        failures.append("security_eval_failed")
    unique_failures = list(dict.fromkeys(failures))
    if unique_failures:
        metrics["regression_count"] = float(len(unique_failures))
    return MissionEvalResult(
        suite_id=suite.suite_id,
        subject_id=str(subject_id or subject.get("task_id") or "mission-artifacts"),
        passed=not unique_failures,
        metrics=metrics,
        failures=unique_failures,
        artifact_refs=_artifact_refs(artifacts, suite.required_artifacts),
        created_at=created_at or _utc_now(),
    )


def _normalize_result(result: MissionEvalResult | dict[str, Any]) -> MissionEvalResult:
    if isinstance(result, MissionEvalResult):
        return result
    return MissionEvalResult.model_validate(result)


def compare_mission_eval_results(
    baseline_result: MissionEvalResult | dict[str, Any],
    candidate_result: MissionEvalResult | dict[str, Any],
    *,
    requires_operator_approval: bool = True,
) -> MissionRegressionGateResult:
    """Compare baseline and candidate eval results for future promotion gates."""

    baseline = _normalize_result(baseline_result)
    candidate = _normalize_result(candidate_result)
    blocked_reasons: list[str] = []
    if baseline.suite_id != candidate.suite_id:
        blocked_reasons.append("suite_mismatch")
    if not candidate.passed:
        blocked_reasons.append("candidate_eval_failed")
    if candidate.metrics.get("artifact_shape_compatible", 0.0) < 1.0:
        blocked_reasons.append("artifact_shape_incompatible")
    if candidate.metrics.get("security_eval_pass_rate", 1.0) < 1.0:
        blocked_reasons.append("security_eval_failed")

    metric_deltas: dict[str, float] = {}
    metric_names = set(baseline.metrics) | set(candidate.metrics)
    for metric_name in sorted(metric_names):
        baseline_value = float(baseline.metrics.get(metric_name, 0.0))
        candidate_value = float(candidate.metrics.get(metric_name, 0.0))
        delta = candidate_value - baseline_value
        metric_deltas[metric_name] = delta
        if metric_name in _HIGHER_IS_BETTER_METRICS and delta < 0:
            blocked_reasons.append(f"metric_regressed:{metric_name}")
        if metric_name in _LOWER_IS_BETTER_METRICS and delta > 0:
            blocked_reasons.append(f"metric_regressed:{metric_name}")

    if baseline.passed and not candidate.passed:
        blocked_reasons.append("regression_failure")

    unique_reasons = list(dict.fromkeys(blocked_reasons))
    return MissionRegressionGateResult(
        baseline_result=baseline.model_dump(mode="json"),
        candidate_result=candidate.model_dump(mode="json"),
        passed=not unique_reasons,
        blocked_reasons=unique_reasons,
        metric_deltas=metric_deltas,
        requires_operator_approval=requires_operator_approval,
    )
