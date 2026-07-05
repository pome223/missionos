"""Vehicle-agnostic read-only review for MissionOS mission episodes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MISSION_EPISODE_REVIEW_SCHEMA_VERSION = "missionos_mission_episode_review.v1"

MissionEpisodeReviewStatus = Literal["passed", "blocked"]
MissionEpisodeReviewSeverity = Literal["info", "warning", "blocking"]

_FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "action",
        "actuator",
        "command",
        "dispatch_authority_created",
        "execute",
        "mavlink_command",
        "mission_upload",
        "raw_velocity",
        "ros_action",
        "setpoint",
    }
)


class MissionEpisodeReviewError(RuntimeError):
    """Raised when a generic mission episode review is not claim-safe."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


_FORBIDDEN_DETAIL_KEYS_NORMALIZED = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_DETAIL_KEYS
)


def _command_like_detail_paths(value: Any, *, root: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_key(key_text) in _FORBIDDEN_DETAIL_KEYS_NORMALIZED:
                paths.append(path)
            paths.extend(_command_like_detail_paths(nested, root=path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            path = f"{root}.{index}" if root else str(index)
            paths.extend(_command_like_detail_paths(nested, root=path))
    return paths


def _raise_for_command_like_detail(value: Any, *, root: str) -> None:
    findings = _command_like_detail_paths(value, root=root)
    if findings:
        raise MissionEpisodeReviewError(
            "mission episode review refused command-like detail keys: "
            + ", ".join(sorted(findings))
        )


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return f"{prefix}_{sha256(encoded).hexdigest()[:12]}"


def _as_tuple(values: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


class MissionEpisodeReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str
    reason: str
    severity: MissionEpisodeReviewSeverity
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_detail(self) -> "MissionEpisodeReviewFinding":
        _raise_for_command_like_detail(self.detail, root="finding.detail")
        return self


class MissionEpisodeReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["missionos_mission_episode_review.v1"] = (
        MISSION_EPISODE_REVIEW_SCHEMA_VERSION
    )
    review_id: str
    source_ref: str
    vehicle_kind: str
    execution_target: str
    execution_mode: str
    source_status: str
    status: MissionEpisodeReviewStatus
    passed: bool
    source_completion_claimed: bool
    source_completion_scope: str
    source_robot_motion_observed: bool
    source_mission_delivery_completion_claimed: bool
    source_physical_execution_invoked: bool
    buckets: tuple[str, ...] = ()
    blocked_buckets: tuple[str, ...] = ()
    warning_buckets: tuple[str, ...] = ()
    findings: tuple[MissionEpisodeReviewFinding, ...] = ()
    evaluated_at: datetime = Field(default_factory=_utc_now)
    review_only: Literal[True] = True
    rule_based: Literal[True] = True
    llm_judge_used: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    approval_created: Literal[False] = False
    progress_counted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False

    @model_validator(mode="after")
    def _validate_review(self) -> "MissionEpisodeReview":
        if self.passed != (self.status == "passed"):
            raise MissionEpisodeReviewError("mission episode review status mismatch")
        if self.status == "passed" and self.blocked_buckets:
            raise MissionEpisodeReviewError(
                "passed mission episode review cannot include blocked buckets"
            )
        if self.source_ref.strip() == "":
            raise MissionEpisodeReviewError("mission episode review requires source ref")
        return self


def mission_episode_review_ref(review: MissionEpisodeReview) -> str:
    return f"mission_episode_review:{review.review_id}"


def build_mission_episode_review(
    *,
    source_summary: Mapping[str, Any],
    source_ref: str,
    vehicle_kind: str,
) -> MissionEpisodeReview:
    """Review a mission summary without creating dispatch or approval authority."""

    source_status = str(source_summary.get("status") or "unknown")
    execution_mode = str(source_summary.get("execution_mode") or "unknown")
    completion_claimed = source_summary.get("completion_claimed") is True
    completion_scope = str(source_summary.get("completion_scope") or "none")
    robot_motion_observed = source_summary.get("robot_motion_observed") is True
    mission_delivery_completion_claimed = (
        source_summary.get("mission_delivery_completion_claimed") is True
    )
    source_physical_execution_invoked = (
        source_summary.get("physical_execution_invoked") is True
    )
    findings: list[MissionEpisodeReviewFinding] = []
    blocked_buckets: set[str] = set()
    warning_buckets: set[str] = set()

    def add_finding(
        *,
        bucket: str,
        reason: str,
        severity: MissionEpisodeReviewSeverity,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        if severity == "blocking":
            blocked_buckets.add(bucket)
        elif severity == "warning":
            warning_buckets.add(bucket)
        findings.append(
            MissionEpisodeReviewFinding(
                bucket=bucket,
                reason=reason,
                severity=severity,
                detail=dict(detail or {}),
            )
        )

    if source_status == "blocked":
        add_finding(
            bucket="episode_blocked",
            reason="Source mission episode remained blocked.",
            severity="blocking",
            detail={
                "source_status": source_status,
                "source_blocking_reasons": list(
                    source_summary.get("blocking_reasons") or []
                ),
            },
        )
    if source_physical_execution_invoked:
        add_finding(
            bucket="physical_execution_claimed",
            reason="Source summary claimed physical execution.",
            severity="blocking",
        )
    if mission_delivery_completion_claimed:
        add_finding(
            bucket="mission_delivery_completion_claimed",
            reason="Source summary claimed mission delivery completion.",
            severity="blocking",
        )
    if execution_mode == "sim" and completion_claimed and completion_scope != "sim_action":
        add_finding(
            bucket="sim_completion_scope_mismatch",
            reason="Simulator completion must stay scoped to sim_action.",
            severity="blocking",
            detail={"completion_scope": completion_scope},
        )
    if completion_claimed and not robot_motion_observed:
        add_finding(
            bucket="completion_without_robot_motion",
            reason="Completion was claimed without robot motion observation.",
            severity="blocking",
        )
    if source_summary.get("nav2_log_diagnostics_status") == "ready":
        add_finding(
            bucket="nav2_diagnostics_available",
            reason="Read-only Nav2 diagnostics were attached to the episode.",
            severity="info",
            detail={
                "observed_pattern_count": len(
                    source_summary.get("nav2_log_observed_patterns") or []
                ),
                "failure_hypothesis_count": len(
                    source_summary.get("nav2_log_failure_hypotheses") or []
                ),
            },
        )
    if (
        source_status == "recovered"
        and source_summary.get("recovery_completion_claimed") is True
    ):
        add_finding(
            bucket="bounded_recovery_completed",
            reason="A bounded recovery action completed without delivery completion.",
            severity="info",
        )

    passed = not blocked_buckets and source_status in {"completed", "recovered"}
    status: MissionEpisodeReviewStatus = "passed" if passed else "blocked"
    buckets = {finding.bucket for finding in findings}
    payload = {
        "source_ref": source_ref,
        "vehicle_kind": vehicle_kind,
        "execution_target": str(source_summary.get("execution_target") or "unknown"),
        "execution_mode": execution_mode,
        "source_status": source_status,
        "status": status,
        "blocked_buckets": sorted(blocked_buckets),
        "warning_buckets": sorted(warning_buckets),
        "completion_claimed": completion_claimed,
        "completion_scope": completion_scope,
        "robot_motion_observed": robot_motion_observed,
        "mission_delivery_completion_claimed": mission_delivery_completion_claimed,
        "source_physical_execution_invoked": source_physical_execution_invoked,
    }
    return MissionEpisodeReview(
        review_id=_stable_id("mission_episode_review", payload),
        source_ref=source_ref,
        vehicle_kind=vehicle_kind,
        execution_target=payload["execution_target"],
        execution_mode=execution_mode,
        source_status=source_status,
        status=status,
        passed=passed,
        source_completion_claimed=completion_claimed,
        source_completion_scope=completion_scope,
        source_robot_motion_observed=robot_motion_observed,
        source_mission_delivery_completion_claimed=(
            mission_delivery_completion_claimed
        ),
        source_physical_execution_invoked=source_physical_execution_invoked,
        buckets=_as_tuple(buckets),
        blocked_buckets=_as_tuple(blocked_buckets),
        warning_buckets=_as_tuple(warning_buckets),
        findings=tuple(findings),
    )


__all__ = [
    "MISSION_EPISODE_REVIEW_SCHEMA_VERSION",
    "MissionEpisodeReview",
    "MissionEpisodeReviewError",
    "MissionEpisodeReviewFinding",
    "build_mission_episode_review",
    "mission_episode_review_ref",
]
