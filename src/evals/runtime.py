"""Phase 0 trajectory-native eval runner."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.computer_use.trajectory_store import get_computer_trajectory_store
from src.evals.failure_taxonomy import PHASE0_FAILURE_BUCKETS, normalize_trajectory_failure
from src.runtime.durable_execution_schema import (
    CheckpointBudget,
    DurableArtifactRef,
    DurableCheckpoint,
    DurableEscalationRecord,
    DurableJobRun,
    DurableJobRunStatus,
    DurableResumeState,
    DurableTaskGraph,
    DurableTaskNode,
    DurableTaskNodeStatus,
    DurableVerifierVerdict,
    DurableVerifierVerdictValue,
    EscalationStatus,
    GuardrailBudgetPolicy,
    RecoveryActionType,
    RecoveryBudgetImpact,
    RecoveryDecision,
    RecoveryPolicy,
    SchedulerQueueEntry,
    SchedulerQueueKind,
    SchedulerQueueState,
)
from src.runtime.orchestration_policy import (
    append_scheduler_queue_entry,
    budget_exhaustion_reasons,
    build_escalation_record,
    default_guardrail_budget_policy,
    default_recovery_policies,
    job_run_status_from_verdict,
    recovery_policy_for_failure_type,
    repair_depth_increment,
    scheduler_available_at,
    scheduler_queue_reason,
    task_node_status_from_verdict,
)
from src.runtime.task_store import get_task_store
from src.tools.memory import get_memory_store
from src.tools.self_improvement_runtime.promotion import REUSE_MEMORY_KINDS
from src.tools.self_improvement_runtime.reuse import prefilter_reuse_payload


class EvalVerifyMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url_contains: str | None = None
    text_contains: str | None = None


class EvalRequestMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    selector_contains: str | None = None
    verify: EvalVerifyMatch = Field(default_factory=EvalVerifyMatch)


class EvalMatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str | None = None
    final_surface_any: list[str] = Field(default_factory=list)
    status_any: list[str] = Field(default_factory=list)
    request: EvalRequestMatch = Field(default_factory=EvalRequestMatch)

    @field_validator("final_surface_any", "status_any", mode="before")
    @classmethod
    def _normalize_text_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]


class EvalSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    goal: str
    surfaces: list[str] = Field(default_factory=list)
    success_criteria: list[Any] = Field(default_factory=list)
    runs: int = Field(default=1, ge=1)
    failure_buckets: list[str] = Field(default_factory=list)
    slice_type: str = Field(default="bounded_long_running")
    substrate: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    match: EvalMatch = Field(default_factory=EvalMatch)

    @field_validator("surfaces", "failure_buckets", "substrate", "expected_artifacts", mode="before")
    @classmethod
    def _normalize_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]


def load_eval_spec(spec_path: str | Path) -> EvalSpec:
    payload = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("eval spec must decode to a YAML object")
    return EvalSpec.model_validate(payload)


def _contains_casefold(value: Any, expected: str | None) -> bool:
    text = str(expected or "").strip()
    if not text:
        return True
    return text.casefold() in str(value or "").casefold()


def _trajectory_matches_spec(trajectory: dict[str, Any], spec: EvalSpec) -> bool:
    match = spec.match
    request = trajectory.get("request") or {}
    request_verify = request.get("verify") or {}

    if match.action and str(trajectory.get("action") or "").strip() != match.action:
        return False
    if match.final_surface_any and str(trajectory.get("final_surface") or "").strip() not in match.final_surface_any:
        return False
    if match.status_any and str(trajectory.get("status") or "").strip() not in match.status_any:
        return False
    if match.request.selector_contains and not _contains_casefold(
        request.get("selector"),
        match.request.selector_contains,
    ):
        return False
    if match.request.verify.url_contains and not _contains_casefold(
        request_verify.get("url_contains"),
        match.request.verify.url_contains,
    ):
        return False
    if match.request.verify.text_contains and not _contains_casefold(
        request_verify.get("text_contains"),
        match.request.verify.text_contains,
    ):
        return False
    return True


def _criteria_summary(spec: EvalSpec, trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    verification = trajectory.get("verification")
    if isinstance(verification, dict):
        checks = verification.get("checks")
        if isinstance(checks, list):
            return [
                {
                    "name": str(item.get("name") or ""),
                    "passed": bool(item.get("passed")),
                    "expected": item.get("expected"),
                }
                for item in checks
                if isinstance(item, dict)
            ]
    return [
        {
            "name": f"criterion_{index + 1}",
            "passed": None,
            "expected": criterion,
        }
        for index, criterion in enumerate(spec.success_criteria)
    ]


def _recommended_repair_targets(failure_type: str | None) -> list[str]:
    mapping = {
        "weak_evidence": [
            "strengthen destination-bound verifier",
            "capture stronger post-action text or screenshot evidence",
        ],
        "focus_mismatch": [
            "record and verify frontmost app before action",
            "strengthen current-tab and desktop focus recovery",
        ],
        "wrong_surface": [
            "rebind the task to the intended execution surface before acting",
            "record preferred surface and enforce surface switch before retry",
        ],
        "target_context_mismatch": [
            "bind action to destination URL or window before typing",
            "strengthen current-tab context preservation and replay checks",
        ],
        "unknown": [
            "inspect replay trace and attempts for a more specific bucket",
        ],
    }
    return mapping.get(str(failure_type or "unknown"), mapping["unknown"])


def _candidate_promotion_artifacts(failure_type: str | None) -> list[str]:
    mapping = {
        "weak_evidence": ["approved_improvement_memory", "approved_skill"],
        "focus_mismatch": ["approved_improvement_memory", "approved_skill"],
        "wrong_surface": ["approved_improvement_memory", "approved_skill"],
        "target_context_mismatch": ["approved_improvement_memory", "capability_patch"],
        "unknown": ["approved_improvement_memory"],
    }
    return list(mapping.get(str(failure_type or "unknown"), mapping["unknown"]))


def _utc_datetime(value: Any) -> datetime:
    timestamp = float(value) if value is not None else None
    if timestamp is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _verifier_evidence_refs(trajectory: dict[str, Any]) -> list[str]:
    verification = trajectory.get("verification")
    if not isinstance(verification, dict):
        return []

    refs: list[str] = []
    for field in ("evidence_refs", "artifact_refs", "screenshot_refs"):
        values = verification.get(field)
        if isinstance(values, list):
            refs.extend(str(item).strip() for item in values if str(item).strip())
    for item in verification.get("checks") or []:
        if not isinstance(item, dict):
            continue
        values = item.get("evidence_refs")
        if isinstance(values, list):
            refs.extend(str(entry).strip() for entry in values if str(entry).strip())
    deduped: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        deduped.append(ref)
    return deduped


def _durable_verdict_value(
    trajectory: dict[str, Any],
    *,
    failure_type: str | None,
) -> DurableVerifierVerdictValue:
    status = str(trajectory.get("status") or "").strip()
    verification = trajectory.get("verification")
    verification_status = (
        str(verification.get("status") or "").strip()
        if isinstance(verification, dict)
        else ""
    )
    if status in {"success", "recovered"} or verification_status == "pass":
        return DurableVerifierVerdictValue.PASS
    if verification_status == "partial_pass" or str(failure_type or "") == "weak_evidence":
        return DurableVerifierVerdictValue.UNCERTAIN
    return DurableVerifierVerdictValue.FAIL


def _durable_verdict_confidence(
    trajectory: dict[str, Any],
    *,
    verdict: DurableVerifierVerdictValue,
) -> tuple[float, str]:
    verification = trajectory.get("verification")
    if isinstance(verification, dict):
        raw = verification.get("confidence")
        try:
            if raw is not None:
                value = float(raw)
                if 0.0 <= value <= 1.0:
                    return value, "reported"
        except (TypeError, ValueError):
            pass
    if verdict == DurableVerifierVerdictValue.PASS:
        return 0.95, "synthetic_default"
    if verdict == DurableVerifierVerdictValue.UNCERTAIN:
        return 0.45, "synthetic_default"
    return 0.85, "synthetic_default"


def _build_durable_verifier_verdict(
    trajectory: dict[str, Any],
    *,
    failure_type: str | None,
    replay_reference: dict[str, Any],
    recommended_repair_targets: list[str],
) -> DurableVerifierVerdict:
    verdict = _durable_verdict_value(trajectory, failure_type=failure_type)
    verification = trajectory.get("verification")
    confidence, confidence_source = _durable_verdict_confidence(
        trajectory,
        verdict=verdict,
    )
    verifier_source = (
        str(verification.get("source") or "").strip()
        if isinstance(verification, dict)
        else ""
    ) or "trajectory_eval_phase0"
    if verdict == DurableVerifierVerdictValue.UNSAFE:
        verifier_source = verifier_source or "future_physical_verifier"
    return DurableVerifierVerdict(
        verdict=verdict,
        evidence_refs=_verifier_evidence_refs(trajectory),
        failure_type=failure_type,
        confidence=confidence,
        confidence_source=confidence_source,
        verifier_source=verifier_source,
        recommended_repair_target=(
            recommended_repair_targets[0] if recommended_repair_targets else None
        ),
        trajectory_id=(
            int(trajectory["id"]) if trajectory.get("id") is not None else None
        ),
        replay_reference=replay_reference,
        created_at=_utc_datetime(trajectory.get("created_at")),
    )


def _default_guardrail_budget_policy(spec: EvalSpec) -> GuardrailBudgetPolicy:
    return default_guardrail_budget_policy(spec.budget if isinstance(spec.budget, dict) else {})


def _base_recovery_decision_for_report(
    report: dict[str, Any],
    *,
    verifier_verdict: DurableVerifierVerdict,
) -> tuple[str | None, RecoveryPolicy | None, RecoveryActionType | None, SchedulerQueueKind]:
    failure_type = report.get("failure_type")
    if verifier_verdict.verdict == DurableVerifierVerdictValue.PASS:
        return failure_type, None, None, SchedulerQueueKind.COMPLETED

    normalized_failure_type = str(failure_type or "unknown")
    policy = recovery_policy_for_failure_type(normalized_failure_type)
    chosen_action = None
    next_queue = SchedulerQueueKind.BLOCKED
    if policy is not None:
        chosen_action = policy.allowed_actions[0] if policy.allowed_actions else None
        next_queue = policy.next_scheduler_queue

    if verifier_verdict.verdict == DurableVerifierVerdictValue.UNCERTAIN:
        chosen_action = RecoveryActionType.REQUEST_HUMAN_APPROVAL
        next_queue = SchedulerQueueKind.WAITING_FOR_APPROVAL
    elif verifier_verdict.verdict == DurableVerifierVerdictValue.UNSAFE:
        chosen_action = RecoveryActionType.REQUEST_HUMAN_APPROVAL
        next_queue = SchedulerQueueKind.WAITING_FOR_APPROVAL

    return normalized_failure_type, policy, chosen_action, next_queue


def _build_run_report_entry(
    trajectory: dict[str, Any],
    spec: EvalSpec,
    *,
    run_index: int,
    get_memory_store_fn: Callable[[], Any],
) -> dict[str, Any]:
    classification = normalize_trajectory_failure(trajectory, classified_by="replay_analysis")
    reuse = prefilter_reuse_payload(
        trajectory,
        limit=3,
        get_memory_store_fn=get_memory_store_fn,
    )
    verification = trajectory.get("verification") if isinstance(trajectory.get("verification"), dict) else {}
    replay_reference = {
        "trajectory_id": trajectory.get("id"),
        "status": trajectory.get("status"),
        "final_surface": trajectory.get("final_surface"),
        "created_at": trajectory.get("created_at"),
    }
    recommended_repair_targets = _recommended_repair_targets(classification["failure_type"])
    candidate_promotion_artifacts = _candidate_promotion_artifacts(classification["failure_type"])
    verifier_verdict = _build_durable_verifier_verdict(
        trajectory,
        failure_type=classification["failure_type"],
        replay_reference=replay_reference,
        recommended_repair_targets=recommended_repair_targets,
    )
    return {
        "run_index": run_index,
        "run_job_id": f"{spec.id}/run-{run_index}",
        "trajectory_id": trajectory.get("id"),
        "status": trajectory.get("status"),
        "final_surface": trajectory.get("final_surface"),
        "failure_type": classification["failure_type"],
        "preliminary_failure_type": classification["preliminary_failure_type"],
        "normalized_failure_type": classification["normalized_failure_type"],
        "classified_by": classification["classified_by"],
        "operator_override": classification["operator_override"],
        "verification_status": verification.get("status") or "",
        "verifier_result": {
            "status": verification.get("status") or "",
            "success": (
                bool(verification.get("success"))
                if "success" in verification
                else None
            ),
            "check_count": (
                len(verification.get("checks") or [])
                if isinstance(verification.get("checks"), list)
                else 0
            ),
        },
        "criteria": _criteria_summary(spec, trajectory),
        "recommended_repair_targets": recommended_repair_targets,
        "candidate_promotion_artifacts": candidate_promotion_artifacts,
        "reuse_query": {
            "query": reuse.get("query", ""),
            "kinds": list(REUSE_MEMORY_KINDS),
            "strategy": "prefilter",
        },
        "reuse_suggestions": reuse.get("results", []),
        "reuse_memory_ids": reuse.get("memory_ids", []),
        "reuse_policy": reuse.get("policy", {}),
        "reuse_trace": (
            trajectory.get("reuse_trace")
            if isinstance(trajectory.get("reuse_trace"), dict)
            else {}
        ),
        "replay_reference": replay_reference,
        "replay": replay_reference,
        "verifier_verdict": verifier_verdict.model_dump(mode="json"),
    }


def _attach_durable_execution_artifacts(
    spec: EvalSpec,
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    # Phase 0 derives these substrate artifacts from completed eval reports so the
    # contract is explicit before a live scheduler-backed runtime exists. The
    # scheduler / recovery / budget / escalation artifacts here are therefore
    # eval-derived orchestration representations, not worker-owned state.
    graph_id = f"{spec.id}/task-graph"
    graph_created_at = datetime.now(timezone.utc)
    budget_policy = _default_guardrail_budget_policy(spec)
    queue_state = SchedulerQueueState()
    escalations: list[DurableEscalationRecord] = []
    recovery_policies = {
        key: value.model_dump(mode="json")
        for key, value in default_recovery_policies().items()
    }
    task_nodes: list[DurableTaskNode] = []
    job_runs: list[DurableJobRun] = []
    checkpoints: list[DurableCheckpoint] = []
    successful_artifacts: dict[str, list[DurableArtifactRef]] = {}
    retry_counters: Counter[str] = Counter()
    llm_calls_used = 0
    tool_calls_used = 0
    repair_depth_used = 0
    pending_approvals_count = 0

    for index, report in enumerate(reports, start=1):
        verifier_verdict = DurableVerifierVerdict.model_validate(
            report.get("verifier_verdict") or {}
        )
        failure_type = str(report.get("failure_type") or "") or None
        checkpoint_id = f"{report['run_job_id']}/checkpoint"
        trajectory_id = report.get("trajectory_id")
        replay_reference = (
            report.get("replay_reference")
            if isinstance(report.get("replay_reference"), dict)
            else {}
        )
        retry_count = retry_counters[failure_type] if failure_type else 0
        (
            normalized_failure_type,
            recovery_policy,
            chosen_action,
            next_scheduler_queue,
        ) = _base_recovery_decision_for_report(
            report,
            verifier_verdict=verifier_verdict,
        )
        llm_calls_used += 1 + (
            recovery_policy.budget_impact.llm_calls if recovery_policy is not None else 0
        )
        tool_calls_used += max(1, len(verifier_verdict.evidence_refs)) + (
            recovery_policy.budget_impact.tool_calls if recovery_policy is not None else 0
        )
        repair_depth_used += repair_depth_increment(chosen_action)
        pending_approvals_after = pending_approvals_count + (
            1 if next_scheduler_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL else 0
        )
        budget_reasons = budget_exhaustion_reasons(
            budget_policy=budget_policy,
            runtime_hours_used=index,
            llm_calls_used=llm_calls_used,
            tool_calls_used=tool_calls_used,
            repair_depth_used=repair_depth_used,
            pending_approvals_count=pending_approvals_after,
            next_scheduler_queue=next_scheduler_queue,
            failure_type=normalized_failure_type,
            retry_count=retry_count,
        )
        budget_exhausted = bool(budget_reasons)
        if budget_exhausted:
            next_scheduler_queue = SchedulerQueueKind.BLOCKED
        recovery_decision = RecoveryDecision(
            node_id=report["run_job_id"],
            failure_type=normalized_failure_type,
            chosen_action=chosen_action,
            policy=recovery_policy,
            next_scheduler_queue=next_scheduler_queue,
            budget_exhausted=budget_exhausted,
            budget_exhausted_reasons=budget_reasons,
        )
        queue_reason = scheduler_queue_reason(
            recovery_decision,
            verifier_verdict=verifier_verdict,
        )
        available_at = scheduler_available_at(
            next_scheduler_queue,
            created_at=graph_created_at,
        )
        artifacts = [
            DurableArtifactRef(
                kind="trajectory",
                ref=str(trajectory_id or ""),
                label=f"trajectory:{trajectory_id}",
                metadata={"trajectory_id": trajectory_id},
            ),
            DurableArtifactRef(
                kind="replay_reference",
                ref=json.dumps(replay_reference, ensure_ascii=True, sort_keys=True),
                label="replay_reference",
                metadata=replay_reference,
            ),
            DurableArtifactRef(
                kind="verifier_verdict",
                ref=f"{report['run_job_id']}/verdict",
                label=verifier_verdict.verdict.value,
                metadata={
                    "failure_type": verifier_verdict.failure_type,
                    "recommended_repair_target": verifier_verdict.recommended_repair_target,
                },
            ),
        ]
        task_nodes.append(
            DurableTaskNode(
                node_id=report["run_job_id"],
                title=f"Evaluate trajectory {trajectory_id}",
                description=f"Bounded eval job for trajectory {trajectory_id} in {spec.id}.",
                status=task_node_status_from_verdict(verifier_verdict.verdict),
                completion_criteria=[
                    str(item.get("name") or "")
                    for item in report.get("criteria") or []
                    if str(item.get("name") or "").strip()
                ],
                artifacts=artifacts,
                retry_count=retry_count,
                next_retry_at=available_at,
                scheduler_queue=next_scheduler_queue,
                trajectory_ids=[int(trajectory_id)] if trajectory_id is not None else [],
                replay_references=[replay_reference] if replay_reference else [],
                checkpoint_refs=[checkpoint_id],
                verifier_verdict=verifier_verdict,
            )
        )
        queue_entry = SchedulerQueueEntry(
            entry_id=f"{report['run_job_id']}/queue",
            node_id=report["run_job_id"],
            queue=next_scheduler_queue,
            reason=queue_reason,
            available_at=available_at,
            checkpoint_id=checkpoint_id,
            trajectory_ids=[int(trajectory_id)] if trajectory_id is not None else [],
            metadata={
                "failure_type": normalized_failure_type,
                "chosen_action": chosen_action.value if chosen_action is not None else "",
            },
        )
        escalation_record = None
        if next_scheduler_queue == SchedulerQueueKind.WAITING_FOR_APPROVAL:
            escalation_record = build_escalation_record(
                run_id=report["run_job_id"],
                node_id=report["run_job_id"],
                checkpoint_id=checkpoint_id,
                created_at=graph_created_at,
                failure_type=normalized_failure_type,
                reason=queue_reason,
            )
            queue_entry.escalation_id = escalation_record.escalation_id
            escalations.append(escalation_record)
            pending_approvals_count = pending_approvals_after
        append_scheduler_queue_entry(queue_state, queue_entry)
        if failure_type and verifier_verdict.verdict != DurableVerifierVerdictValue.PASS:
            retry_counters[failure_type] += 1
        current_pending_approval_ids = [
            escalation.approval_request_id
            for escalation in escalations
            if escalation.approval_request_id
        ]

        report["recovery_policy"] = (
            recovery_policy.model_dump(mode="json") if recovery_policy is not None else None
        )
        report["recovery_decision"] = recovery_decision.model_dump(mode="json")
        report["scheduler_queue"] = next_scheduler_queue.value
        report["scheduler_queue_entry"] = queue_entry.model_dump(mode="json")
        report["escalation_record"] = (
            escalation_record.model_dump(mode="json") if escalation_record is not None else None
        )
        report["_checkpoint_budget_snapshot"] = {
            "runtime_hours_used": index,
            "llm_calls_used": llm_calls_used,
            "tool_calls_used": tool_calls_used,
            "same_failure_retries": dict(retry_counters),
            "repair_depth_used": repair_depth_used,
            "pending_approvals_count": pending_approvals_count,
            "pending_approval_ids": current_pending_approval_ids,
        }

    task_graph = DurableTaskGraph(
        graph_id=graph_id,
        goal=spec.goal,
        nodes=task_nodes,
        created_at=graph_created_at,
        updated_at=graph_created_at,
        metadata={
            "eval_id": spec.id,
            "slice_type": spec.slice_type,
            "scheduler_phase": "eval_derived_phase0",
        },
    )

    for index, (report, task_node) in enumerate(zip(reports, task_nodes, strict=False), start=1):
        verifier_verdict = task_node.verifier_verdict
        checkpoint_id = task_node.checkpoint_refs[0] if task_node.checkpoint_refs else None
        checkpoint_budget_snapshot = (
            report.get("_checkpoint_budget_snapshot")
            if isinstance(report.get("_checkpoint_budget_snapshot"), dict)
            else {}
        )
        pending_approval_ids = [
            str(item).strip()
            for item in checkpoint_budget_snapshot.get("pending_approval_ids") or []
            if str(item).strip()
        ]
        same_failure_retry_counts = (
            checkpoint_budget_snapshot.get("same_failure_retries")
            if isinstance(checkpoint_budget_snapshot.get("same_failure_retries"), dict)
            else {}
        )
        if task_node.status == DurableTaskNodeStatus.DONE:
            successful_artifacts[task_node.node_id] = list(task_node.artifacts)

        checkpoint = DurableCheckpoint(
            checkpoint_id=checkpoint_id or f"{task_node.node_id}/checkpoint",
            graph_id=task_graph.graph_id,
            run_id=report["run_job_id"],
            current_goal=spec.goal,
            current_task_node_id=task_node.node_id,
            open_task_node_ids=task_graph.open_task_node_ids(),
            blocked_task_node_ids=task_graph.blocked_task_node_ids(),
            pending_approval_ids=list(pending_approval_ids),
            last_successful_artifacts=dict(successful_artifacts),
            budget=CheckpointBudget(
                run_budget_remaining=max(0, len(reports) - index),
                retry_budget_remaining={
                    failure_key: max(
                        0,
                        budget_policy.max_same_failure_retries - retry_total,
                    )
                    for failure_key, retry_total in same_failure_retry_counts.items()
                },
                policy=budget_policy,
                runtime_hours_used=int(checkpoint_budget_snapshot.get("runtime_hours_used") or index),
                llm_calls_used=int(checkpoint_budget_snapshot.get("llm_calls_used") or 0),
                tool_calls_used=int(checkpoint_budget_snapshot.get("tool_calls_used") or 0),
                same_failure_retries=dict(same_failure_retry_counts),
                repair_depth_used=int(checkpoint_budget_snapshot.get("repair_depth_used") or 0),
                pending_approvals_count=int(
                    checkpoint_budget_snapshot.get("pending_approvals_count") or len(pending_approval_ids)
                ),
                budget_exhausted=bool(
                    report.get("recovery_decision", {}).get("budget_exhausted")
                ),
                budget_exhausted_reasons=list(
                    report.get("recovery_decision", {}).get("budget_exhausted_reasons") or []
                ),
            ),
            retry_counters={
                node.node_id: node.retry_count
                for node in task_nodes
                if node.retry_count > 0
            },
            trajectory_ids=task_node.trajectory_ids,
            replay_references=list(task_node.replay_references),
            next_actionable_task_node_id=task_graph.next_actionable_task_node_id(),
            created_at=graph_created_at,
        )
        job_run = DurableJobRun(
            run_id=report["run_job_id"],
            graph_id=task_graph.graph_id,
            node_id=task_node.node_id,
            goal=spec.goal,
            status=job_run_status_from_verdict(
                verifier_verdict.verdict if verifier_verdict is not None else DurableVerifierVerdictValue.FAIL
            ),
            attempt=task_node.retry_count + 1,
            trajectory_id=(task_node.trajectory_ids[0] if task_node.trajectory_ids else None),
            replay_reference=(
                task_node.replay_references[0] if task_node.replay_references else {}
            ),
            checkpoint_id=checkpoint.checkpoint_id,
            scheduler_queue=task_node.scheduler_queue,
            verifier_verdict=verifier_verdict,
            started_at=graph_created_at,
            ended_at=graph_created_at,
        )
        checkpoints.append(checkpoint)
        job_runs.append(job_run)

        report["task_node_id"] = task_node.node_id
        report["task_node"] = task_node.model_dump(mode="json")
        report["checkpoint_id"] = checkpoint.checkpoint_id
        report["checkpoint"] = checkpoint.model_dump(mode="json")
        report["job_run"] = job_run.model_dump(mode="json")
        report["budget_state"] = checkpoint.budget.model_dump(mode="json")
        report.pop("_checkpoint_budget_snapshot", None)

    resume_state = (
        checkpoints[-1].resume_state(task_graph)
        if checkpoints
        else DurableResumeState(
            checkpoint_id=f"{graph_id}/checkpoint",
            graph_id=graph_id,
            reason="graph_empty",
        )
    )
    final_pending_approval_ids = [
        escalation.approval_request_id
        for escalation in escalations
        if escalation.approval_request_id
    ]
    resume_state = resume_state.model_copy(
        update={
            "pending_approval_ids": list(final_pending_approval_ids),
            "scheduler_queue_counts": queue_state.counts(),
        }
    )

    return {
        "task_graph": task_graph.model_dump(mode="json"),
        "job_runs": [item.model_dump(mode="json") for item in job_runs],
        "checkpoints": [item.model_dump(mode="json") for item in checkpoints],
        "verifier_verdicts": [
            node.verifier_verdict.model_dump(mode="json")
            for node in task_nodes
            if node.verifier_verdict is not None
        ],
        "recovery_policies": recovery_policies,
        "scheduler_state": queue_state.model_dump(mode="json"),
        "escalations": [item.model_dump(mode="json") for item in escalations],
        "resume_state": resume_state.model_dump(mode="json"),
    }


def _persist_failure_classification(store: Any, trajectory: dict[str, Any]) -> dict[str, Any]:
    classification = normalize_trajectory_failure(trajectory, classified_by="replay_analysis")
    if (
        trajectory.get("preliminary_failure_type") != classification["preliminary_failure_type"]
        or trajectory.get("normalized_failure_type") != classification["normalized_failure_type"]
        or list(trajectory.get("classified_by") or []) != classification["classified_by"]
        or trajectory.get("operator_override") != classification["operator_override"]
    ):
        store.update_failure_classification(
            int(trajectory["id"]),
            preliminary_failure_type=classification["preliminary_failure_type"],
            normalized_failure_type=classification["normalized_failure_type"],
            classified_by=classification["classified_by"],
            operator_override=classification["operator_override"],
        )
        updated = store.get(int(trajectory["id"]))
        if updated is not None:
            return updated
    return {
        **trajectory,
        **classification,
    }


def _without_operator_override(trajectory: dict[str, Any]) -> dict[str, Any]:
    classified_by = [
        str(item).strip()
        for item in (trajectory.get("classified_by") or [])
        if str(item).strip() and str(item).strip() != "operator"
    ]
    return {
        **trajectory,
        "normalized_failure_type": None,
        "operator_override": None,
        "classified_by": classified_by,
    }


def override_trajectory_failure_type(
    trajectory_id: int,
    *,
    failure_type: str | None,
    get_trajectory_store_fn: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    resolved_trajectory_store_fn = get_trajectory_store_fn or get_computer_trajectory_store
    store = resolved_trajectory_store_fn()
    trajectory = store.get(int(trajectory_id))
    if trajectory is None:
        return {
            "success": False,
            "error": f"Unknown computer trajectory: {trajectory_id}",
        }

    requested_override = str(failure_type or "").strip() or None
    if requested_override is not None and requested_override not in PHASE0_FAILURE_BUCKETS:
        return {
            "success": False,
            "error": f"Unsupported failure type override: {requested_override}",
        }

    baseline = normalize_trajectory_failure(_without_operator_override(trajectory))
    classified_by = [
        str(item).strip()
        for item in (baseline.get("classified_by") or [])
        if str(item).strip()
    ]
    if requested_override and "operator" not in classified_by:
        classified_by.append("operator")

    normalized_failure_type = requested_override or baseline.get("normalized_failure_type")
    updated = store.update_failure_classification(
        int(trajectory_id),
        preliminary_failure_type=baseline.get("preliminary_failure_type"),
        normalized_failure_type=normalized_failure_type,
        classified_by=classified_by,
        operator_override=requested_override,
    )
    if not updated:
        return {
            "success": False,
            "error": f"Failed to update computer trajectory: {trajectory_id}",
        }

    refreshed = store.get(int(trajectory_id))
    return {
        "success": True,
        "trajectory": refreshed,
        "trajectory_id": trajectory_id,
        "operator_override": requested_override,
    }


def _failure_bucket_delta(
    current: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, dict[str, int]]:
    keys = sorted(
        {
            *[str(key) for key in (current or {}).keys()],
            *[str(key) for key in (baseline or {}).keys()],
        }
    )
    return {
        key: {
            "current": int((current or {}).get(key, 0) or 0),
            "baseline": int((baseline or {}).get(key, 0) or 0),
            "delta": int((current or {}).get(key, 0) or 0) - int((baseline or {}).get(key, 0) or 0),
        }
        for key in keys
    }


def compare_eval_reports(
    current_report: dict[str, Any],
    baseline_report: dict[str, Any],
) -> dict[str, Any]:
    current_buckets = current_report.get("failure_buckets") if isinstance(current_report, dict) else {}
    baseline_buckets = baseline_report.get("failure_buckets") if isinstance(baseline_report, dict) else {}
    failure_buckets = _failure_bucket_delta(
        current_buckets if isinstance(current_buckets, dict) else {},
        baseline_buckets if isinstance(baseline_buckets, dict) else {},
    )
    improved = [
        key
        for key, item in failure_buckets.items()
        if int(item["delta"]) < 0
    ]
    regressed = [
        key
        for key, item in failure_buckets.items()
        if int(item["delta"]) > 0
    ]
    current_success_rate = float(current_report.get("success_rate") or 0.0)
    baseline_success_rate = float(baseline_report.get("success_rate") or 0.0)
    current_runs = int(current_report.get("runs_evaluated") or 0)
    baseline_runs = int(baseline_report.get("runs_evaluated") or 0)
    return {
        "success_rate": {
            "current": round(current_success_rate, 4),
            "baseline": round(baseline_success_rate, 4),
            "delta": round(current_success_rate - baseline_success_rate, 4),
        },
        "runs_evaluated": {
            "current": current_runs,
            "baseline": baseline_runs,
            "delta": current_runs - baseline_runs,
        },
        "failure_buckets": failure_buckets,
        "improved_buckets": improved,
        "regressed_buckets": regressed,
    }


def _resolve_eval_task(
    store: Any,
    *,
    task_id: str | None = None,
    eval_id: str | None = None,
) -> dict[str, Any] | None:
    if task_id:
        return store.get(task_id)
    if not eval_id:
        return None

    matching_terminal_tasks: list[dict[str, Any]] = []
    for status in ("completed", "failed"):
        recent = store.query(
            kind="eval_run",
            status=status,
            page=1,
            page_size=100,
        )["tasks"]
        for item in recent:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if str(metadata.get("eval_id") or "") == str(eval_id):
                matching_terminal_tasks.append(item)

    if not matching_terminal_tasks:
        return None

    matching_terminal_tasks.sort(
        key=lambda item: (
            float(item.get("created_at") or 0.0),
            float(item.get("updated_at") or 0.0),
        ),
        reverse=True,
    )
    return matching_terminal_tasks[0]


def _select_trajectories(
    store: Any,
    spec: EvalSpec,
    *,
    trajectory_id: int | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    if trajectory_id is not None:
        trajectory = store.get(int(trajectory_id))
        return [trajectory] if trajectory is not None and _trajectory_matches_spec(trajectory, spec) else []

    requested = max(1, int(limit or spec.runs))
    candidates = store.recent(limit=max(requested * 10, 50))
    matches = [trajectory for trajectory in candidates if _trajectory_matches_spec(trajectory, spec)]
    return matches[:requested]


def run_eval_spec(
    spec_path: str | Path,
    *,
    trajectory_id: int | None = None,
    limit: int | None = None,
    get_trajectory_store_fn: Callable[[], Any] | None = None,
    get_task_store_fn: Callable[[], Any] | None = None,
    get_memory_store_fn: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    spec = load_eval_spec(spec_path)
    resolved_trajectory_store_fn = get_trajectory_store_fn or get_computer_trajectory_store
    resolved_task_store_fn = get_task_store_fn or get_task_store
    resolved_memory_store_fn = get_memory_store_fn or get_memory_store

    store = resolved_trajectory_store_fn()
    trajectories = _select_trajectories(
        store,
        spec,
        trajectory_id=trajectory_id,
        limit=limit,
    )

    task_store = resolved_task_store_fn()
    task = task_store.create(
        kind="eval_run",
        title=f"Eval run {spec.id}",
        status="running",
        owner_session_id="local_cli",
        owner_user_id="local_cli",
        artifacts={
            "spec": spec.model_dump(mode="json"),
            "spec_path": str(spec_path),
        },
        metadata={
            "eval_id": spec.id,
            "trajectory_id": trajectory_id,
        },
    )

    if not trajectories:
        error = "No trajectories matched this eval spec"
        task_store.update(
            task["task_id"],
            status="failed",
            artifacts={
                "report": {
                    "success": False,
                    "eval_id": spec.id,
                    "goal": spec.goal,
                    "spec_path": str(spec_path),
                    "runs_requested": max(1, int(limit or spec.runs)),
                    "runs_evaluated": 0,
                    "success_rate": 0.0,
                    "failure_buckets": {},
                    "reports": [],
                    "error": error,
                }
            },
            error=error,
        )
        return {
            "success": False,
            "task_id": task["task_id"],
            "error": error,
        }

    normalized_runs = [_persist_failure_classification(store, trajectory) for trajectory in trajectories]
    reports = [
        _build_run_report_entry(
            trajectory,
            spec,
            run_index=index + 1,
            get_memory_store_fn=resolved_memory_store_fn,
        )
        for index, trajectory in enumerate(normalized_runs)
    ]
    durable_execution = _attach_durable_execution_artifacts(spec, reports)
    success_count = sum(1 for item in reports if item["status"] in {"success", "recovered"})
    buckets = Counter(
        item["failure_type"]
        for item in reports
        if item.get("failure_type")
    )
    configured = spec.failure_buckets or list(PHASE0_FAILURE_BUCKETS)
    failure_buckets = {
        bucket: int(buckets.get(bucket, 0))
        for bucket in configured
        if buckets.get(bucket, 0) or bucket in configured
    }

    report = {
        "success": True,
        "eval_id": spec.id,
        "goal": spec.goal,
        "spec_path": str(spec_path),
        "slice": {
            "type": spec.slice_type,
            "substrate": spec.substrate
            or [
                "task_store",
                "trajectory_store",
                "replay_report",
                "scheduler_queue_state",
                "recovery_policy",
                "guardrail_budget",
                "human_escalation_queue",
                "approval_gated_promotion",
            ],
            "expected_artifacts": spec.expected_artifacts
            or [
                "trajectory_id",
                "verifier_result",
                "failure_type",
                "recommended_repair_targets",
                "candidate_promotion_artifacts",
                "replay_reference",
                "reuse_memory_ids",
                "reuse_suggestions",
                "recovery_policy",
                "recovery_decision",
                "budget_state",
                "scheduler_queue_entry",
                "escalation_record",
                "task_graph",
                "job_run",
                "checkpoint",
                "verifier_verdict",
                "scheduler_state",
            ],
        },
        "runs_requested": max(1, int(limit or spec.runs)),
        "runs_evaluated": len(reports),
        "success_rate": round(success_count / len(reports), 4),
        "failure_buckets": failure_buckets,
        "durable_execution": durable_execution,
        "run_jobs": reports,
        "reports": reports,
    }
    task_store.update(
        task["task_id"],
        status="completed",
        artifacts={"report": report},
    )
    return {
        "success": True,
        "task_id": task["task_id"],
        **report,
    }


def get_eval_report(
    *,
    task_id: str | None = None,
    eval_id: str | None = None,
    compare_to_task_id: str | None = None,
    compare_to_eval_id: str | None = None,
    get_task_store_fn: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    if not task_id and not eval_id:
        return {
            "success": False,
            "error": "task_id or eval_id is required",
        }

    store = (get_task_store_fn or get_task_store)()
    task = _resolve_eval_task(store, task_id=task_id, eval_id=eval_id)

    if task is None:
        return {
            "success": False,
            "error": "Eval report not found",
        }

    artifacts = task.get("artifacts") if isinstance(task.get("artifacts"), dict) else {}
    report = artifacts.get("report") if isinstance(artifacts.get("report"), dict) else {}
    spec = artifacts.get("spec") if isinstance(artifacts.get("spec"), dict) else {}
    payload = {
        "success": True,
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "spec": spec,
        "report": report,
    }
    if compare_to_task_id or compare_to_eval_id:
        baseline_task = _resolve_eval_task(
            store,
            task_id=compare_to_task_id,
            eval_id=compare_to_eval_id,
        )
        if baseline_task is None:
            return {
                "success": False,
                "error": "Comparison eval report not found",
            }
        baseline_artifacts = (
            baseline_task.get("artifacts")
            if isinstance(baseline_task.get("artifacts"), dict)
            else {}
        )
        baseline_report = (
            baseline_artifacts.get("report")
            if isinstance(baseline_artifacts.get("report"), dict)
            else {}
        )
        payload["comparison"] = compare_eval_reports(report, baseline_report)
        payload["compare_to_task_id"] = baseline_task.get("task_id")
        payload["compare_to_eval_id"] = (
            baseline_task.get("metadata", {}).get("eval_id")
            if isinstance(baseline_task.get("metadata"), dict)
            else None
        )
    return payload
