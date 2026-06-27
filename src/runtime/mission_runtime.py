"""Mission Runtime artifacts built on the existing task substrate."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MISSION_SCORECARD_SCHEMA_VERSION = "mission_scorecard.v1"
MISSION_REVIEW_SCHEMA_VERSION = "mission_review.v1"
MEMORY_PROMOTION_CANDIDATE_SCHEMA_VERSION = "memory_promotion_candidate.v1"
MISSION_MEMORY_LINKS_SCHEMA_VERSION = "mission_memory_links.v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


class MissionObjectiveProgress(str, Enum):
    UNKNOWN = "unknown"
    PARTIAL = "partial"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"


class MemoryPromotionApprovalStatus(str, Enum):
    CANDIDATE_ONLY = "candidate_only"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MissionScorecard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MISSION_SCORECARD_SCHEMA_VERSION
    objective_progress: MissionObjectiveProgress = MissionObjectiveProgress.UNKNOWN
    verification_pass_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    recovery_success_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    approval_wait_count: int = Field(default=0, ge=0)
    repeated_failure_count: int = Field(default=0, ge=0)
    blocked_count: int = Field(default=0, ge=0)
    improvement_candidate_count: int = Field(default=0, ge=0)
    memory_promotion_candidate_count: int = Field(default=0, ge=0)
    last_verifier_verdict: str | None = None
    updated_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MissionImprovementCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    candidate_type: str
    failure_type: str | None = None
    summary: str
    requires_benchmark: bool = True
    requires_approval: bool = True
    source_artifact_ref: str
    approval_status: str = "candidate_only"
    metadata: dict[str, Any] = Field(default_factory=dict)


class MissionMemoryPromotionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MEMORY_PROMOTION_CANDIDATE_SCHEMA_VERSION
    candidate_id: str
    type: str
    content: str
    source_task_id: str | None = None
    source_artifact_ref: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    last_verified_at: datetime
    expires_at: datetime | None = None
    promotion_reason: str
    invalidation_rule: str = ""
    approval_required: bool = True
    approval_status: MemoryPromotionApprovalStatus = (
        MemoryPromotionApprovalStatus.CANDIDATE_ONLY
    )
    approved_by: str = ""
    approved_at: datetime | None = None
    rejected_reason: str = ""
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MissionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MISSION_REVIEW_SCHEMA_VERSION
    mission_task_id: str = ""
    summary: str
    final_status: str
    scorecard_snapshot: dict[str, Any]
    failure_buckets: list[dict[str, Any]] = Field(default_factory=list)
    repeated_failure_patterns: list[dict[str, Any]] = Field(default_factory=list)
    recovery_effectiveness: dict[str, Any] = Field(default_factory=dict)
    evidence_quality: dict[str, Any] = Field(default_factory=dict)
    improvement_candidates: list[dict[str, Any]] = Field(default_factory=list)
    memory_promotion_candidates: list[dict[str, Any]] = Field(default_factory=list)
    recommended_next_contract_edits: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)


def _nodes(durable_execution: dict[str, Any]) -> list[dict[str, Any]]:
    task_graph = _as_dict(durable_execution.get("task_graph"))
    return [_as_dict(item) for item in _as_list(task_graph.get("nodes"))]


def _job_runs(durable_execution: dict[str, Any]) -> list[dict[str, Any]]:
    return [_as_dict(item) for item in _as_list(durable_execution.get("job_runs"))]


def _verdicts(durable_execution: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = [
        _as_dict(item) for item in _as_list(durable_execution.get("verifier_verdicts"))
    ]
    if explicit:
        return explicit
    verdicts: list[dict[str, Any]] = []
    for job in _job_runs(durable_execution):
        verdict = _as_dict(job.get("verifier_verdict"))
        if verdict:
            verdicts.append(verdict)
    for node in _nodes(durable_execution):
        verdict = _as_dict(node.get("verifier_verdict"))
        if verdict:
            verdicts.append(verdict)
    return verdicts


def _failure_counts(durable_execution: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for verdict in _verdicts(durable_execution):
        failure_type = str(verdict.get("failure_type") or "").strip()
        verdict_value = str(verdict.get("verdict") or "").strip()
        if failure_type and verdict_value != "pass":
            counts[failure_type] += 1
    return counts


def _recovery_decisions(durable_execution: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _as_dict(item) for item in _as_list(durable_execution.get("recovery_decisions"))
    ]


def _counter_by_key(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in items:
        value = str(item.get(key) or "").strip()
        if value:
            counts[value] += 1
    return dict(sorted(counts.items()))


def _artifact_source_refs(
    durable_execution: dict[str, Any],
    *,
    source_task_id: str | None,
    child_task_ids: list[str] | tuple[str, ...] | None,
) -> list[str]:
    refs: list[str] = []

    def add(value: object) -> None:
        ref = str(value or "").strip()
        if ref and ref not in refs:
            refs.append(ref)

    add(f"task:{source_task_id}" if source_task_id else "")
    contract_id = str(
        _as_dict(durable_execution.get("mission_contract")).get("contract_id") or ""
    )
    add(f"mission_contract:{contract_id}" if contract_id else "")

    for child_task_id in child_task_ids or []:
        add(f"child_task:{child_task_id}")
    for run in _job_runs(durable_execution):
        add(f"job_run:{run.get('run_id')}" if run.get("run_id") else "")
        replay = _as_dict(run.get("replay_reference"))
        add(
            f"child_task:{replay.get('child_task_id')}"
            if replay.get("child_task_id")
            else ""
        )
        verdict = _as_dict(run.get("verifier_verdict"))
        for evidence_ref in _str_list(verdict.get("evidence_refs")):
            add(evidence_ref)
    for verdict in _verdicts(durable_execution):
        for evidence_ref in _str_list(verdict.get("evidence_refs")):
            add(evidence_ref)
        replay = _as_dict(verdict.get("replay_reference"))
        add(
            f"child_task:{replay.get('child_task_id')}"
            if replay.get("child_task_id")
            else ""
        )
    for checkpoint in _as_list(durable_execution.get("checkpoints")):
        checkpoint_dict = _as_dict(checkpoint)
        add(
            f"checkpoint:{checkpoint_dict.get('checkpoint_id')}"
            if checkpoint_dict.get("checkpoint_id")
            else ""
        )
        for replay_ref in _as_list(checkpoint_dict.get("replay_references")):
            replay = _as_dict(replay_ref)
            add(
                f"child_task:{replay.get('child_task_id')}"
                if replay.get("child_task_id")
                else ""
            )
    for decision in _recovery_decisions(durable_execution):
        for source_ref in _str_list(decision.get("source_refs")):
            add(source_ref)
    for escalation in _as_list(durable_execution.get("escalations")):
        escalation_dict = _as_dict(escalation)
        add(
            f"escalation:{escalation_dict.get('escalation_id')}"
            if escalation_dict.get("escalation_id")
            else ""
        )
        add(
            f"approval:{escalation_dict.get('approval_request_id')}"
            if escalation_dict.get("approval_request_id")
            else ""
        )
    return refs


def _evidence_quality(durable_execution: dict[str, Any]) -> dict[str, Any]:
    verdicts = _verdicts(durable_execution)
    total_verdicts = len(verdicts)
    evidence_ref_count = 0
    verdicts_with_evidence = 0
    weak_evidence_count = 0
    unsafe_count = 0
    failed_without_evidence_count = 0
    for verdict in verdicts:
        verdict_value = str(verdict.get("verdict") or "").strip()
        failure_type = str(verdict.get("failure_type") or "").strip()
        evidence_refs = _str_list(verdict.get("evidence_refs"))
        if evidence_refs:
            verdicts_with_evidence += 1
            evidence_ref_count += len(evidence_refs)
        if failure_type == "weak_evidence":
            weak_evidence_count += 1
        if verdict_value == "unsafe":
            unsafe_count += 1
        if verdict_value in {"fail", "uncertain", "unsafe"} and not evidence_refs:
            failed_without_evidence_count += 1

    coverage_rate = verdicts_with_evidence / total_verdicts if total_verdicts else 0.0
    if not verdicts:
        quality = "unknown"
    elif weak_evidence_count or failed_without_evidence_count:
        quality = "weak"
    elif unsafe_count:
        quality = "unsafe"
    elif coverage_rate >= 0.8:
        quality = "strong"
    else:
        quality = "mixed"

    return {
        "quality": quality,
        "total_verifier_verdicts": total_verdicts,
        "verdicts_with_evidence": verdicts_with_evidence,
        "evidence_ref_count": evidence_ref_count,
        "evidence_coverage_rate": coverage_rate,
        "weak_evidence_count": weak_evidence_count,
        "unsafe_count": unsafe_count,
        "failed_without_evidence_count": failed_without_evidence_count,
    }


def _objective_progress(
    *,
    nodes: list[dict[str, Any]],
    final_status: str,
) -> MissionObjectiveProgress:
    statuses = {str(node.get("status") or "").strip() for node in nodes}
    if final_status == "completed" or (statuses and statuses <= {"done"}):
        return MissionObjectiveProgress.SATISFIED
    if final_status in {"blocked", "failed"} or "blocked" in statuses:
        return MissionObjectiveProgress.BLOCKED
    if "done" in statuses:
        return MissionObjectiveProgress.PARTIAL
    return MissionObjectiveProgress.UNKNOWN


def _memory_policy(durable_execution: dict[str, Any]) -> dict[str, Any]:
    contract = _as_dict(durable_execution.get("mission_contract"))
    policy = _as_dict(contract.get("memory_policy"))
    return {
        "promote_only": _str_list(policy.get("promote_only"))
        or [
            "fact",
            "procedure",
            "failure_pattern",
            "recovery_pattern",
            "approved_improvement",
            "mission_summary",
        ],
        "never_promote": _str_list(policy.get("never_promote"))
        or [
            "raw_transcript",
            "secret",
            "one_off_noise",
        ],
        "require_operator_approval": bool(
            policy.get("require_operator_approval", True)
        ),
        "candidate_ttl_seconds": policy.get("candidate_ttl_seconds", 2_592_000),
    }


def _normalize_approval_status(value: Any) -> MemoryPromotionApprovalStatus:
    if isinstance(value, MemoryPromotionApprovalStatus):
        return value
    text = str(value or "").strip().lower()
    for status in MemoryPromotionApprovalStatus:
        if text == status.value:
            return status
    return MemoryPromotionApprovalStatus.CANDIDATE_ONLY


def _candidate_source_refs(
    *,
    source_task_id: str | None,
    source_artifact_ref: str,
    existing_refs: list[Any] | None = None,
) -> list[str]:
    refs: list[str] = []

    def add(value: object) -> None:
        ref = str(value or "").strip()
        if ref and ref not in refs:
            refs.append(ref)

    for ref in existing_refs or []:
        add(ref)
    add(f"task:{source_task_id}" if source_task_id else "")
    add(f"mission_review:{source_task_id}" if source_task_id else "")
    add(source_artifact_ref)
    return refs


def _memory_candidate_is_expired(
    candidate: MissionMemoryPromotionCandidate,
    *,
    now: datetime,
) -> bool:
    return candidate.expires_at is not None and candidate.expires_at <= now


def normalize_memory_promotion_candidate(
    candidate: MissionMemoryPromotionCandidate | dict[str, Any],
    *,
    default_status: (
        MemoryPromotionApprovalStatus | str
    ) = MemoryPromotionApprovalStatus.CANDIDATE_ONLY,
    source_task_id: str | None = None,
    source_artifact_ref: str | None = None,
    now: datetime | None = None,
) -> MissionMemoryPromotionCandidate:
    payload = (
        candidate.model_dump(mode="python")
        if isinstance(candidate, MissionMemoryPromotionCandidate)
        else dict(_as_dict(candidate))
    )
    resolved_source_task_id = (
        str(source_task_id or payload.get("source_task_id") or "").strip() or None
    )
    resolved_source_ref = str(
        source_artifact_ref
        or payload.get("source_artifact_ref")
        or "mission_review.memory_promotion_candidates"
    ).strip()
    metadata = dict(_as_dict(payload.get("metadata")))
    if not payload.get("expires_at"):
        metadata.setdefault(
            "no_expiry_reason", "legacy candidate did not include expires_at"
        )
    status = _normalize_approval_status(
        payload.get("approval_status") or default_status
    )
    if status == MemoryPromotionApprovalStatus.CANDIDATE_ONLY:
        status = _normalize_approval_status(default_status)

    payload.update(
        {
            "schema_version": str(
                payload.get("schema_version")
                or MEMORY_PROMOTION_CANDIDATE_SCHEMA_VERSION
            ),
            "candidate_id": str(payload.get("candidate_id") or "").strip(),
            "type": str(payload.get("type") or "failure_pattern").strip(),
            "content": str(payload.get("content") or "").strip(),
            "source_task_id": resolved_source_task_id,
            "source_artifact_ref": resolved_source_ref,
            "confidence": float(payload.get("confidence") or 0.0),
            "last_verified_at": payload.get("last_verified_at") or (now or _utc_now()),
            "promotion_reason": str(
                payload.get("promotion_reason") or "mission review candidate"
            ).strip(),
            "invalidation_rule": str(
                payload.get("invalidation_rule")
                or "Invalidate when the source mission review is superseded."
            ).strip(),
            "approval_required": bool(payload.get("approval_required", True)),
            "approval_status": status,
            "approved_by": str(payload.get("approved_by") or "").strip(),
            "approved_at": payload.get("approved_at"),
            "rejected_reason": str(payload.get("rejected_reason") or "").strip(),
            "source_refs": _candidate_source_refs(
                source_task_id=resolved_source_task_id,
                source_artifact_ref=resolved_source_ref,
                existing_refs=_as_list(payload.get("source_refs")),
            ),
            "metadata": metadata,
        }
    )
    normalized = MissionMemoryPromotionCandidate(**payload)
    current_time = now or _utc_now()
    if _memory_candidate_is_expired(
        normalized, now=current_time
    ) and normalized.approval_status in {
        MemoryPromotionApprovalStatus.CANDIDATE_ONLY,
        MemoryPromotionApprovalStatus.PENDING,
    }:
        normalized = normalized.model_copy(
            update={"approval_status": MemoryPromotionApprovalStatus.EXPIRED}
        )
    return normalized


def approve_memory_promotion_candidate(
    candidate: MissionMemoryPromotionCandidate | dict[str, Any],
    *,
    approved_by: str,
    approved_at: datetime | None = None,
) -> MissionMemoryPromotionCandidate:
    approver = str(approved_by or "").strip()
    if not approver:
        raise ValueError("approved_by is required")
    current_time = approved_at or _utc_now()
    normalized = normalize_memory_promotion_candidate(
        candidate,
        default_status=MemoryPromotionApprovalStatus.PENDING,
        now=current_time,
    )
    if normalized.approval_status == MemoryPromotionApprovalStatus.EXPIRED:
        raise ValueError("expired memory promotion candidates cannot be approved")
    return normalized.model_copy(
        update={
            "approval_status": MemoryPromotionApprovalStatus.APPROVED,
            "approved_by": approver,
            "approved_at": current_time,
            "rejected_reason": "",
        }
    )


def reject_memory_promotion_candidate(
    candidate: MissionMemoryPromotionCandidate | dict[str, Any],
    *,
    rejected_reason: str,
    now: datetime | None = None,
) -> MissionMemoryPromotionCandidate:
    reason = str(rejected_reason or "").strip()
    if not reason:
        raise ValueError("rejected_reason is required")
    normalized = normalize_memory_promotion_candidate(
        candidate,
        default_status=MemoryPromotionApprovalStatus.PENDING,
        now=now or _utc_now(),
    )
    return normalized.model_copy(
        update={
            "approval_status": MemoryPromotionApprovalStatus.REJECTED,
            "approved_by": "",
            "approved_at": None,
            "rejected_reason": reason,
        }
    )


def is_memory_promotion_candidate_approved(
    candidate: MissionMemoryPromotionCandidate | dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    current_time = now or _utc_now()
    normalized = normalize_memory_promotion_candidate(candidate, now=current_time)
    return (
        normalized.approval_status == MemoryPromotionApprovalStatus.APPROVED
        and not _memory_candidate_is_expired(normalized, now=current_time)
    )


def is_memory_promotion_candidate_approval_ready(
    candidate: MissionMemoryPromotionCandidate | dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    normalized = normalize_memory_promotion_candidate(
        candidate,
        default_status=MemoryPromotionApprovalStatus.PENDING,
        now=now or _utc_now(),
    )
    return (
        normalized.approval_required
        and normalized.approval_status == MemoryPromotionApprovalStatus.PENDING
    )


def approval_ready_memory_promotion_candidates(
    candidates: list[MissionMemoryPromotionCandidate | dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[MissionMemoryPromotionCandidate]:
    current_time = now or _utc_now()
    return [
        normalize_memory_promotion_candidate(
            candidate,
            default_status=MemoryPromotionApprovalStatus.PENDING,
            now=current_time,
        )
        for candidate in candidates
        if is_memory_promotion_candidate_approval_ready(
            candidate,
            now=current_time,
        )
    ]


def build_memory_promotion_candidate_artifacts(
    review: MissionReview | dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    review_payload = (
        review.model_dump(mode="python")
        if isinstance(review, MissionReview)
        else _as_dict(review)
    )
    source_task_id = str(review_payload.get("mission_task_id") or "").strip() or None
    current_time = now or _utc_now()
    artifacts: list[dict[str, Any]] = []
    for candidate in _as_list(review_payload.get("memory_promotion_candidates")):
        normalized = normalize_memory_promotion_candidate(
            _as_dict(candidate),
            default_status=MemoryPromotionApprovalStatus.PENDING,
            source_task_id=source_task_id,
            source_artifact_ref=str(
                _as_dict(candidate).get("source_artifact_ref")
                or "mission_review.memory_promotion_candidates"
            ),
            now=current_time,
        )
        artifacts.append(normalized.model_dump(mode="json"))
    return artifacts


def build_memory_promotion_candidates(
    durable_execution: dict[str, Any],
    *,
    source_task_id: str | None = None,
    now: datetime | None = None,
) -> list[MissionMemoryPromotionCandidate]:
    current_time = now or _utc_now()
    policy = _memory_policy(durable_execution)
    promote_only = set(policy["promote_only"])
    never_promote = set(policy["never_promote"])
    if "failure_pattern" not in promote_only or "failure_pattern" in never_promote:
        return []

    ttl = policy.get("candidate_ttl_seconds")
    expires_at = None
    if isinstance(ttl, int) and ttl > 0:
        expires_at = current_time + timedelta(seconds=ttl)

    candidates: list[MissionMemoryPromotionCandidate] = []
    contract_id = str(
        _as_dict(durable_execution.get("mission_contract")).get("contract_id")
        or "mission"
    )
    for failure_type, count in sorted(_failure_counts(durable_execution).items()):
        confidence = min(0.9, 0.55 + (0.1 * max(1, count)))
        candidates.append(
            MissionMemoryPromotionCandidate(
                candidate_id=f"{contract_id}:failure_pattern:{failure_type}",
                type="failure_pattern",
                content=(
                    f"Mission observed failure pattern '{failure_type}' "
                    f"{count} time(s); review recovery and verifier behavior before promotion."
                ),
                source_task_id=source_task_id,
                source_artifact_ref="durable_execution.verifier_verdicts",
                confidence=confidence,
                last_verified_at=current_time,
                expires_at=expires_at,
                promotion_reason="repeated or terminal mission failure evidence",
                invalidation_rule=(
                    "Invalidate when the failure taxonomy, verifier contract, or mission "
                    "contract changes materially."
                ),
                approval_required=bool(policy["require_operator_approval"]),
                metadata={
                    "failure_type": failure_type,
                    "count": count,
                    "policy_source": "mission_contract.memory_policy",
                },
            )
        )
    return candidates


def build_mission_scorecard(
    durable_execution: dict[str, Any],
    *,
    final_status: str = "",
    updated_at: datetime | None = None,
) -> MissionScorecard:
    current_time = updated_at or _utc_now()
    nodes = _nodes(durable_execution)
    job_runs = _job_runs(durable_execution)
    verdicts = _verdicts(durable_execution)
    pass_count = sum(1 for verdict in verdicts if verdict.get("verdict") == "pass")
    total_verdicts = len(verdicts)
    failure_counts = _failure_counts(durable_execution)
    blocked_count = sum(
        1
        for node in nodes
        if node.get("status") == "blocked"
        or node.get("scheduler_queue") in {"blocked", "waiting_for_approval"}
    )
    escalations = _as_list(durable_execution.get("escalations"))
    retry_nodes = [node for node in nodes if int(node.get("retry_count") or 0) > 0]
    recovered_nodes = [node for node in retry_nodes if node.get("status") == "done"]
    recovery_success_rate = (
        len(recovered_nodes) / len(retry_nodes) if retry_nodes else 0.0
    )
    last_verdict = None
    if verdicts:
        last_verdict = str(verdicts[-1].get("verdict") or "").strip() or None
    memory_candidates = build_memory_promotion_candidates(
        durable_execution,
        now=current_time,
    )
    return MissionScorecard(
        objective_progress=_objective_progress(
            nodes=nodes,
            final_status=str(final_status or ""),
        ),
        verification_pass_rate=pass_count / total_verdicts if total_verdicts else 0.0,
        recovery_success_rate=recovery_success_rate,
        approval_wait_count=len(escalations),
        repeated_failure_count=sum(
            max(0, count - 1) for count in failure_counts.values()
        ),
        blocked_count=blocked_count,
        improvement_candidate_count=len(failure_counts),
        memory_promotion_candidate_count=len(memory_candidates),
        last_verifier_verdict=last_verdict,
        updated_at=current_time,
        metadata={
            "total_job_runs": len(job_runs),
            "total_verifier_verdicts": total_verdicts,
            "retry_node_count": len(retry_nodes),
            "recovered_retry_node_count": len(recovered_nodes),
            "failure_type_counts": dict(failure_counts),
        },
    )


def _improvement_type(failure_type: str) -> str:
    if failure_type == "weak_evidence":
        return "verifier_improvement"
    if failure_type in {"focus_mismatch", "wrong_surface", "target_context_mismatch"}:
        return "recovery_strategy"
    if failure_type == "policy_blocked":
        return "policy_adjustment"
    if failure_type == "tool_timeout":
        return "benchmark_case"
    return "diagnostic_task"


def _build_improvement_candidates(
    durable_execution: dict[str, Any],
) -> list[MissionImprovementCandidate]:
    contract = _as_dict(durable_execution.get("mission_contract"))
    improvement_policy = _as_dict(contract.get("improvement_policy"))
    requires_benchmark = bool(improvement_policy.get("require_benchmark_pass", True))
    requires_approval = bool(improvement_policy.get("require_human_promotion", True))
    contract_id = str(contract.get("contract_id") or "mission")
    candidates: list[MissionImprovementCandidate] = []
    for failure_type, count in sorted(_failure_counts(durable_execution).items()):
        candidate_type = _improvement_type(failure_type)
        candidates.append(
            MissionImprovementCandidate(
                candidate_id=f"{contract_id}:improvement:{failure_type}",
                candidate_type=candidate_type,
                failure_type=failure_type,
                summary=(
                    f"Create a {candidate_type} candidate for failure type "
                    f"'{failure_type}' observed {count} time(s)."
                ),
                requires_benchmark=requires_benchmark,
                requires_approval=requires_approval,
                source_artifact_ref="durable_execution.verifier_verdicts",
                metadata={"count": count},
            )
        )
    return candidates


def build_mission_review(
    durable_execution: dict[str, Any],
    *,
    final_status: str,
    source_task_id: str | None = None,
    child_task_ids: list[str] | tuple[str, ...] | None = None,
    mission_scorecard: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> MissionReview:
    current_time = created_at or _utc_now()
    scorecard = build_mission_scorecard(
        durable_execution,
        final_status=final_status,
        updated_at=current_time,
    )
    scorecard_snapshot = _as_dict(mission_scorecard) or scorecard.model_dump(
        mode="json"
    )
    failure_counts = _failure_counts(durable_execution)
    failure_buckets = [
        {"failure_type": failure_type, "count": count}
        for failure_type, count in sorted(failure_counts.items())
    ]
    repeated = [item for item in failure_buckets if int(item.get("count") or 0) > 1]
    improvement_candidates = [
        item.model_dump(mode="json")
        for item in _build_improvement_candidates(durable_execution)
    ]
    memory_candidates = [
        item.model_dump(mode="json")
        for item in build_memory_promotion_candidates(
            durable_execution,
            source_task_id=source_task_id,
            now=current_time,
        )
    ]
    contract = _as_dict(durable_execution.get("mission_contract"))
    recommendations: list[str] = []
    if not _str_list(contract.get("success_metrics")):
        recommendations.append("Add explicit success_metrics to the MissionContract.")
    if failure_buckets:
        recommendations.append(
            "Review recovery_policy ladder coverage for observed failure buckets."
        )
    if memory_candidates:
        recommendations.append(
            "Review memory_promotion_candidates before operator-approved promotion."
        )

    recovery_decisions = _recovery_decisions(durable_execution)
    recovery_outcome_counts = _counter_by_key(recovery_decisions, "outcome")
    selected_step_counts = _counter_by_key(recovery_decisions, "selected_step")
    recovery_failure_counts = _counter_by_key(recovery_decisions, "failure_type")
    recovered_count = int(recovery_outcome_counts.get("completed", 0))
    scheduled_count = int(recovery_outcome_counts.get("recovery_scheduled", 0))
    blocked_recovery_count = int(recovery_outcome_counts.get("blocked", 0))
    paused_recovery_count = int(recovery_outcome_counts.get("paused", 0))
    budget_exhausted_count = sum(
        1 for decision in recovery_decisions if bool(decision.get("budget_exhausted"))
    )
    effectiveness = {
        "recovery_success_rate": scorecard.recovery_success_rate,
        "blocked_count": scorecard.blocked_count,
        "approval_wait_count": scorecard.approval_wait_count,
        "recovery_decision_count": len(recovery_decisions),
        "recovery_outcome_counts": recovery_outcome_counts,
        "selected_step_counts": selected_step_counts,
        "failure_type_counts": recovery_failure_counts,
        "recovered_count": recovered_count,
        "scheduled_count": scheduled_count,
        "blocked_recovery_count": blocked_recovery_count,
        "paused_recovery_count": paused_recovery_count,
        "budget_exhausted_count": budget_exhausted_count,
    }
    evidence_quality = _evidence_quality(durable_execution)
    source_refs = _artifact_source_refs(
        durable_execution,
        source_task_id=source_task_id,
        child_task_ids=child_task_ids,
    )
    summary = (
        f"Mission ended as {final_status or 'unknown'} with "
        f"{scorecard.metadata.get('total_job_runs', 0)} job run(s), "
        f"verification_pass_rate={scorecard.verification_pass_rate:.2f}, "
        f"objective_progress={scorecard.objective_progress.value}."
    )
    return MissionReview(
        mission_task_id=str(source_task_id or ""),
        summary=summary,
        final_status=str(final_status or "unknown"),
        scorecard_snapshot=scorecard_snapshot,
        failure_buckets=failure_buckets,
        repeated_failure_patterns=repeated,
        recovery_effectiveness=effectiveness,
        evidence_quality=evidence_quality,
        improvement_candidates=improvement_candidates,
        memory_promotion_candidates=memory_candidates,
        recommended_next_contract_edits=recommendations,
        source_refs=source_refs,
        created_at=current_time,
    )


def build_post_mission_review_artifacts(
    durable_execution: dict[str, Any],
    *,
    final_status: str,
    source_task_id: str | None = None,
    child_task_ids: list[str] | tuple[str, ...] | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    review = build_mission_review(
        durable_execution,
        final_status=final_status,
        source_task_id=source_task_id,
        child_task_ids=child_task_ids,
        created_at=created_at,
    )
    memory_candidate_artifacts = build_memory_promotion_candidate_artifacts(
        review,
        now=review.created_at,
    )
    return {
        "durable_execution": durable_execution,
        "mission_scorecard": review.scorecard_snapshot,
        "mission_review": review.model_dump(mode="json"),
        "memory_promotion_candidates": memory_candidate_artifacts,
        "mission_memory_links": build_mission_memory_links(
            review,
            memory_promotion_candidates=memory_candidate_artifacts,
        ),
    }


def build_mission_memory_links(
    review: MissionReview,
    *,
    memory_promotion_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": MISSION_MEMORY_LINKS_SCHEMA_VERSION,
        "memory_promotion_candidates": (
            memory_promotion_candidates
            if memory_promotion_candidates is not None
            else list(review.memory_promotion_candidates)
        ),
    }
