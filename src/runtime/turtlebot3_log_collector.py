"""Read-only TurtleBot3/Nav2 simulator process-log collector.

The collector records bounded references to simulator process logs. It does not
interpret logs as approval, dispatch, completion, delivery, or physical
execution. Raw log text is not persisted in the artifact; records carry counts,
digests, short excerpts, and refs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TURTLEBOT3_LOG_RECORD_SCHEMA_VERSION = "missionos_turtlebot3_log_record.v1"
TURTLEBOT3_LOG_BUNDLE_SCHEMA_VERSION = "missionos_turtlebot3_log_bundle.v1"
TURTLEBOT3_NAV2_LOG_DIAGNOSTICS_SCHEMA_VERSION = (
    "missionos_turtlebot3_nav2_log_diagnostics.v1"
)
TURTLEBOT3_LOG_BUNDLE_PATHS_ENV = "MISSIONOS_TURTLEBOT3_LOG_BUNDLE_PATHS"
TURTLEBOT3_LOG_BUNDLE_REF_ENV = "MISSIONOS_TURTLEBOT3_LOG_BUNDLE_REF"
DEFAULT_TURTLEBOT3_REQUIRED_LOG_SOURCES = (
    "gazebo",
    "nav2",
    "relay",
    "telemetry_sidecar",
)

LogBundleStatus = Literal["ready", "blocked"]
Nav2LogDiagnosticStatus = Literal["ready", "insufficient_source"]


class TurtleBot3LogCollectorError(RuntimeError):
    """Raised when TurtleBot3 process logs cannot be collected safely."""


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _normalize_source_name(source_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", source_name.strip().lower()).strip("_")
    if not normalized:
        raise TurtleBot3LogCollectorError("log source name cannot be empty")
    if normalized in {
        "cmd_vel",
        "dispatch",
        "execute",
        "goal_pose",
        "navigate_to_pose",
        "physical_execution",
        "raw_velocity",
    }:
        raise TurtleBot3LogCollectorError(
            f"log source name is command-like: {source_name}"
        )
    return normalized


def _meaningful_lines(log_text: str) -> list[str]:
    return [line.strip() for line in log_text.splitlines() if line.strip()]


def _latest_excerpt(log_text: str, *, max_chars: int = 240) -> str:
    lines = _meaningful_lines(log_text)
    if not lines:
        return ""
    return lines[-1].replace("\r", " ").replace("\n", " ")[-max_chars:]


def parse_turtlebot3_log_bundle_paths_env(
    env: Mapping[str, str] | None = None,
) -> dict[str, Path]:
    """Parse the JSON log-source mapping from the environment."""

    source_env = env or os.environ
    raw = str(source_env.get(TURTLEBOT3_LOG_BUNDLE_PATHS_ENV, "")).strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TurtleBot3LogCollectorError(
            f"{TURTLEBOT3_LOG_BUNDLE_PATHS_ENV} must be a JSON object"
        ) from exc
    if not isinstance(payload, Mapping):
        raise TurtleBot3LogCollectorError(
            f"{TURTLEBOT3_LOG_BUNDLE_PATHS_ENV} must be a JSON object"
        )
    paths: dict[str, Path] = {}
    for source_name, path_value in payload.items():
        if not isinstance(path_value, str) or not path_value.strip():
            raise TurtleBot3LogCollectorError(
                f"log path for {source_name!s} must be a non-empty string"
            )
        paths[_normalize_source_name(str(source_name))] = Path(path_value)
    return paths


def turtlebot3_log_bundle_ref_from_paths(
    paths: Mapping[str, str | Path],
    *,
    explicit_ref: str | None = None,
) -> str:
    """Build a stable process-log bundle ref from source labels and paths."""

    if explicit_ref:
        return explicit_ref
    normalized = {
        _normalize_source_name(str(source_name)): str(path)
        for source_name, path in paths.items()
    }
    digest = sha256(
        json.dumps(normalized, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"turtlebot3_process_log_bundle:{digest}"


def turtlebot3_log_bundle_ref_from_env(
    env: Mapping[str, str] | None = None,
) -> str | None:
    source_env = env or os.environ
    explicit = str(source_env.get(TURTLEBOT3_LOG_BUNDLE_REF_ENV, "")).strip() or None
    paths = parse_turtlebot3_log_bundle_paths_env(source_env)
    if not paths and explicit is None:
        return None
    return turtlebot3_log_bundle_ref_from_paths(paths, explicit_ref=explicit)


class TurtleBot3LogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TURTLEBOT3_LOG_RECORD_SCHEMA_VERSION] = (
        TURTLEBOT3_LOG_RECORD_SCHEMA_VERSION
    )
    source_name: str = Field(min_length=1)
    source_path: str | None = None
    log_observed: bool
    raw_log_ref: str | None = None
    sha256: str | None = None
    byte_count: int = Field(ge=0)
    line_count: int = Field(ge=0)
    latest_excerpt: str = ""
    blocked_reasons: tuple[str, ...] = ()

    @field_validator("source_name")
    @classmethod
    def _validate_source_name(cls, value: str) -> str:
        return _normalize_source_name(value)

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _normalize_blocked_reasons(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @model_validator(mode="after")
    def _validate_record(self) -> "TurtleBot3LogRecord":
        if self.log_observed:
            if self.blocked_reasons:
                raise ValueError("observed log record cannot include blocked reasons")
            if not self.raw_log_ref or not self.sha256:
                raise ValueError("observed log record requires ref and sha256")
            if self.byte_count <= 0 or self.line_count <= 0:
                raise ValueError("observed log record requires non-empty text")
        elif not self.blocked_reasons:
            raise ValueError("missing log record requires blocked reasons")
        return self


class TurtleBot3LogBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TURTLEBOT3_LOG_BUNDLE_SCHEMA_VERSION] = (
        TURTLEBOT3_LOG_BUNDLE_SCHEMA_VERSION
    )
    bundle_id: str
    raw_logs_ref: str = Field(min_length=1)
    records: tuple[TurtleBot3LogRecord, ...] = Field(min_length=1)
    source_count: int = Field(ge=1)
    observed_source_count: int = Field(ge=0)
    required_sources: tuple[str, ...] = ()
    missing_required_sources: tuple[str, ...] = ()
    bundle_status: LogBundleStatus
    blocked_reasons: tuple[str, ...] = ()
    created_at: datetime
    simulation_only: Literal[True] = True
    logs_only: Literal[True] = True
    read_only: Literal[True] = True
    raw_logs_included: Literal[False] = False
    command_surface_present: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    raw_velocity_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mission_delivery_completion_claimed: Literal[False] = False

    @field_validator(
        "required_sources",
        "missing_required_sources",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _normalize_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        return _parse_datetime(value) if not isinstance(value, datetime) else _utc(value)

    @model_validator(mode="after")
    def _validate_bundle(self) -> "TurtleBot3LogBundle":
        if self.source_count != len(self.records):
            raise ValueError("source_count must match records")
        observed = sum(1 for record in self.records if record.log_observed)
        if self.observed_source_count != observed:
            raise ValueError("observed_source_count must match records")
        if self.bundle_status == "ready" and self.blocked_reasons:
            raise ValueError("ready log bundle cannot include blocked reasons")
        if self.bundle_status == "blocked" and not self.blocked_reasons:
            raise ValueError("blocked log bundle requires blocked reasons")
        return self


class TurtleBot3Nav2LogDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[TURTLEBOT3_NAV2_LOG_DIAGNOSTICS_SCHEMA_VERSION] = (
        TURTLEBOT3_NAV2_LOG_DIAGNOSTICS_SCHEMA_VERSION
    )
    diagnostic_id: str
    raw_logs_ref: str | None = None
    source_log_ref: str | None = None
    source_path: str | None = None
    diagnostic_status: Nav2LogDiagnosticStatus
    observed_patterns: tuple[str, ...] = ()
    failure_hypotheses: tuple[str, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    goal_received_count: int = Field(ge=0)
    follow_path_abort_count: int = Field(ge=0)
    failed_to_make_progress_count: int = Field(ge=0)
    costmap_clear_count: int = Field(ge=0)
    spin_recovery_count: int = Field(ge=0)
    goal_rejected_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    cancel_signal_count: int = Field(ge=0)
    latest_relevant_excerpt: str = ""
    relevant_excerpt_count: int = Field(ge=0)
    created_at: datetime
    simulation_only: Literal[True] = True
    logs_only: Literal[True] = True
    read_only: Literal[True] = True
    raw_logs_included: Literal[False] = False
    command_surface_present: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    mission_delivery_completion_claimed: Literal[False] = False

    @field_validator(
        "observed_patterns",
        "failure_hypotheses",
        "blocking_reasons",
        mode="before",
    )
    @classmethod
    def _normalize_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        return _parse_datetime(value) if not isinstance(value, datetime) else _utc(value)

    @model_validator(mode="after")
    def _validate_diagnostics(self) -> "TurtleBot3Nav2LogDiagnostics":
        if self.diagnostic_status == "ready":
            if self.blocking_reasons:
                raise ValueError("ready diagnostics cannot include blocking reasons")
            if not self.source_log_ref:
                raise ValueError("ready diagnostics require a source_log_ref")
        elif not self.blocking_reasons:
            raise ValueError("insufficient diagnostics require blocking reasons")
        return self


def _count_pattern(log_text: str, pattern: str) -> int:
    return len(re.findall(pattern, log_text, flags=re.IGNORECASE))


def _relevant_nav2_lines(log_text: str) -> list[str]:
    relevant = []
    patterns = (
        r"failed to make progress",
        r"aborting handle",
        r"goal rejected",
        r"timed? ?out|timeout",
        r"cancel",
        r"clear entirely.*costmap",
        r"running spin|spin completed",
        r"received a goal",
    )
    for line in _meaningful_lines(log_text):
        if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in patterns):
            relevant.append(line.replace("\r", " ").replace("\n", " "))
    return relevant


def _nav2_record(bundle: TurtleBot3LogBundle) -> TurtleBot3LogRecord | None:
    return next(
        (record for record in bundle.records if record.source_name == "nav2"),
        None,
    )


def build_turtlebot3_nav2_log_diagnostics(
    bundle: TurtleBot3LogBundle,
    *,
    now: datetime | None = None,
) -> TurtleBot3Nav2LogDiagnostics:
    """Classify Nav2 process-log signatures without creating authority claims."""

    record = _nav2_record(bundle)
    if record is None:
        return TurtleBot3Nav2LogDiagnostics(
            diagnostic_id=_stable_id(
                "turtlebot3_nav2_log_diagnostics",
                {"raw_logs_ref": bundle.raw_logs_ref, "reason": "nav2_log_missing"},
            ),
            raw_logs_ref=bundle.raw_logs_ref,
            diagnostic_status="insufficient_source",
            blocking_reasons=("nav2_log_record_missing",),
            goal_received_count=0,
            follow_path_abort_count=0,
            failed_to_make_progress_count=0,
            costmap_clear_count=0,
            spin_recovery_count=0,
            goal_rejected_count=0,
            timeout_count=0,
            cancel_signal_count=0,
            created_at=_utc(now),
        )
    if not record.log_observed or not record.source_path:
        return TurtleBot3Nav2LogDiagnostics(
            diagnostic_id=_stable_id(
                "turtlebot3_nav2_log_diagnostics",
                {
                    "raw_logs_ref": bundle.raw_logs_ref,
                    "source_log_ref": record.raw_log_ref,
                    "reason": "nav2_log_unobserved",
                },
            ),
            raw_logs_ref=bundle.raw_logs_ref,
            source_log_ref=record.raw_log_ref,
            source_path=record.source_path,
            diagnostic_status="insufficient_source",
            blocking_reasons=("nav2_log_unobserved",),
            goal_received_count=0,
            follow_path_abort_count=0,
            failed_to_make_progress_count=0,
            costmap_clear_count=0,
            spin_recovery_count=0,
            goal_rejected_count=0,
            timeout_count=0,
            cancel_signal_count=0,
            created_at=_utc(now),
        )
    try:
        log_text = Path(record.source_path).read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return TurtleBot3Nav2LogDiagnostics(
            diagnostic_id=_stable_id(
                "turtlebot3_nav2_log_diagnostics",
                {
                    "raw_logs_ref": bundle.raw_logs_ref,
                    "source_log_ref": record.raw_log_ref,
                    "reason": "nav2_log_unreadable",
                },
            ),
            raw_logs_ref=bundle.raw_logs_ref,
            source_log_ref=record.raw_log_ref,
            source_path=record.source_path,
            diagnostic_status="insufficient_source",
            blocking_reasons=("nav2_log_unreadable",),
            goal_received_count=0,
            follow_path_abort_count=0,
            failed_to_make_progress_count=0,
            costmap_clear_count=0,
            spin_recovery_count=0,
            goal_rejected_count=0,
            timeout_count=0,
            cancel_signal_count=0,
            created_at=_utc(now),
        )

    counts = {
        "goal_received_count": _count_pattern(log_text, r"received a goal"),
        "follow_path_abort_count": _count_pattern(log_text, r"aborting handle"),
        "failed_to_make_progress_count": _count_pattern(
            log_text,
            r"failed to make progress",
        ),
        "costmap_clear_count": _count_pattern(log_text, r"clear entirely.*costmap"),
        "spin_recovery_count": _count_pattern(
            log_text,
            r"running spin|spin completed",
        ),
        "goal_rejected_count": _count_pattern(log_text, r"goal rejected"),
        "timeout_count": _count_pattern(log_text, r"timed? ?out|timeout"),
        "cancel_signal_count": _count_pattern(log_text, r"cancel"),
    }
    observed_patterns: list[str] = []
    failure_hypotheses: list[str] = []
    if counts["goal_received_count"]:
        observed_patterns.append("nav2_goal_received")
    if counts["failed_to_make_progress_count"]:
        observed_patterns.append("controller_failed_to_make_progress")
        failure_hypotheses.append(
            "controller_progress_blocked_or_goal_inside_constrained_costmap"
        )
    if counts["follow_path_abort_count"]:
        observed_patterns.append("follow_path_action_aborted")
        failure_hypotheses.append("follow_path_action_aborted")
    if counts["costmap_clear_count"]:
        observed_patterns.append("costmap_clear_recovery_observed")
        if counts["failed_to_make_progress_count"]:
            failure_hypotheses.append("recovery_goal_stalled_after_costmap_clear")
    if counts["spin_recovery_count"]:
        observed_patterns.append("spin_recovery_behavior_observed")
    if counts["goal_rejected_count"]:
        observed_patterns.append("nav2_goal_rejected")
        failure_hypotheses.append("nav2_goal_rejected_before_execution")
    if counts["timeout_count"]:
        observed_patterns.append("nav2_timeout_signal_observed")
        failure_hypotheses.append("nav2_goal_result_timeout_or_slow_recovery")
    if counts["cancel_signal_count"]:
        observed_patterns.append("nav2_cancel_signal_observed")
        if counts["goal_received_count"] > 1:
            failure_hypotheses.append("goal_transition_or_cancel_overlap_possible")

    relevant_lines = _relevant_nav2_lines(log_text)
    latest_excerpt = relevant_lines[-1][-240:] if relevant_lines else ""
    payload_for_id = {
        "raw_logs_ref": bundle.raw_logs_ref,
        "source_log_ref": record.raw_log_ref,
        "counts": counts,
        "observed_patterns": observed_patterns,
        "failure_hypotheses": failure_hypotheses,
    }
    return TurtleBot3Nav2LogDiagnostics(
        diagnostic_id=_stable_id("turtlebot3_nav2_log_diagnostics", payload_for_id),
        raw_logs_ref=bundle.raw_logs_ref,
        source_log_ref=record.raw_log_ref,
        source_path=record.source_path,
        diagnostic_status="ready",
        observed_patterns=tuple(observed_patterns),
        failure_hypotheses=tuple(failure_hypotheses),
        goal_received_count=counts["goal_received_count"],
        follow_path_abort_count=counts["follow_path_abort_count"],
        failed_to_make_progress_count=counts["failed_to_make_progress_count"],
        costmap_clear_count=counts["costmap_clear_count"],
        spin_recovery_count=counts["spin_recovery_count"],
        goal_rejected_count=counts["goal_rejected_count"],
        timeout_count=counts["timeout_count"],
        cancel_signal_count=counts["cancel_signal_count"],
        latest_relevant_excerpt=latest_excerpt,
        relevant_excerpt_count=len(relevant_lines),
        created_at=_utc(now),
    )


def _record_from_text(
    *,
    source_name: str,
    source_path: Path,
    log_text: str,
) -> TurtleBot3LogRecord:
    normalized = _normalize_source_name(source_name)
    digest = sha256(log_text.encode("utf-8")).hexdigest()
    return TurtleBot3LogRecord(
        source_name=normalized,
        source_path=str(source_path),
        log_observed=True,
        raw_log_ref=f"turtlebot3_process_log:{normalized}:{digest[:16]}",
        sha256=digest,
        byte_count=len(log_text.encode("utf-8")),
        line_count=len(_meaningful_lines(log_text)),
        latest_excerpt=_latest_excerpt(log_text),
    )


def _missing_record(
    *,
    source_name: str,
    source_path: Path | None,
    reason: str,
) -> TurtleBot3LogRecord:
    return TurtleBot3LogRecord(
        source_name=_normalize_source_name(source_name),
        source_path=str(source_path) if source_path is not None else None,
        log_observed=False,
        byte_count=0,
        line_count=0,
        blocked_reasons=(reason,),
    )


def collect_turtlebot3_log_bundle_from_paths(
    paths: Mapping[str, str | Path],
    *,
    required_sources: Sequence[str] = DEFAULT_TURTLEBOT3_REQUIRED_LOG_SOURCES,
    raw_logs_ref: str | None = None,
    now: datetime | None = None,
) -> TurtleBot3LogBundle:
    """Collect bounded refs for TurtleBot3 simulator process logs."""

    normalized_paths = {
        _normalize_source_name(str(source_name)): Path(path)
        for source_name, path in paths.items()
    }
    required = tuple(_normalize_source_name(source) for source in required_sources)
    source_names = tuple(sorted({*normalized_paths.keys(), *required}))
    records: list[TurtleBot3LogRecord] = []
    for source_name in source_names:
        path = normalized_paths.get(source_name)
        if path is None:
            records.append(
                _missing_record(
                    source_name=source_name,
                    source_path=None,
                    reason="log_source_path_missing",
                )
            )
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            records.append(
                _missing_record(
                    source_name=source_name,
                    source_path=path,
                    reason="log_source_unreadable",
                )
            )
            continue
        if not text.strip():
            records.append(
                _missing_record(
                    source_name=source_name,
                    source_path=path,
                    reason="log_source_empty",
                )
            )
            continue
        records.append(
            _record_from_text(source_name=source_name, source_path=path, log_text=text)
        )

    missing_required = tuple(
        source
        for source in required
        if not any(
            record.source_name == source and record.log_observed for record in records
        )
    )
    blocked_reasons: list[str] = []
    if missing_required:
        blocked_reasons.append("required_turtlebot3_process_logs_missing")
    blocked_reasons.extend(
        f"{record.source_name}:{reason}"
        for record in records
        for reason in record.blocked_reasons
    )
    ref = raw_logs_ref or turtlebot3_log_bundle_ref_from_paths(normalized_paths)
    payload_for_id = {
        "raw_logs_ref": ref,
        "records": [
            {
                "source_name": record.source_name,
                "raw_log_ref": record.raw_log_ref,
                "sha256": record.sha256,
                "blocked_reasons": record.blocked_reasons,
            }
            for record in records
        ],
    }
    return TurtleBot3LogBundle(
        bundle_id=_stable_id("turtlebot3_log_bundle", payload_for_id),
        raw_logs_ref=ref,
        records=tuple(records),
        source_count=len(records),
        observed_source_count=sum(1 for record in records if record.log_observed),
        required_sources=required,
        missing_required_sources=missing_required,
        bundle_status="blocked" if blocked_reasons else "ready",
        blocked_reasons=tuple(blocked_reasons),
        created_at=_utc(now),
    )


def collect_turtlebot3_log_bundle_from_env(
    env: Mapping[str, str] | None = None,
    *,
    required_sources: Sequence[str] = DEFAULT_TURTLEBOT3_REQUIRED_LOG_SOURCES,
    now: datetime | None = None,
) -> TurtleBot3LogBundle | None:
    source_env = env or os.environ
    paths = parse_turtlebot3_log_bundle_paths_env(source_env)
    if not paths:
        return None
    ref = turtlebot3_log_bundle_ref_from_env(source_env)
    return collect_turtlebot3_log_bundle_from_paths(
        paths,
        required_sources=required_sources,
        raw_logs_ref=ref,
        now=now,
    )


__all__ = [
    "DEFAULT_TURTLEBOT3_REQUIRED_LOG_SOURCES",
    "TURTLEBOT3_LOG_BUNDLE_PATHS_ENV",
    "TURTLEBOT3_LOG_BUNDLE_REF_ENV",
    "TURTLEBOT3_LOG_BUNDLE_SCHEMA_VERSION",
    "TURTLEBOT3_NAV2_LOG_DIAGNOSTICS_SCHEMA_VERSION",
    "TURTLEBOT3_LOG_RECORD_SCHEMA_VERSION",
    "TurtleBot3LogBundle",
    "TurtleBot3LogCollectorError",
    "TurtleBot3Nav2LogDiagnostics",
    "TurtleBot3LogRecord",
    "build_turtlebot3_nav2_log_diagnostics",
    "collect_turtlebot3_log_bundle_from_env",
    "collect_turtlebot3_log_bundle_from_paths",
    "parse_turtlebot3_log_bundle_paths_env",
    "turtlebot3_log_bundle_ref_from_env",
    "turtlebot3_log_bundle_ref_from_paths",
]
