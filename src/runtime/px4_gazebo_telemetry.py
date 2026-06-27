"""PX4/Gazebo telemetry-only sanitizer.

This module is the first implementation slice for the PX4/Gazebo
telemetry-only simulator introduction path. It accepts static telemetry/log
samples and turns them into a sanitized Mission OS artifact. It deliberately
does not start PX4 or Gazebo, attach task artifacts, create HIL evidence, send
commands, publish ROS messages, send MAVLink payloads, or mutate a simulator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryContract,
    HilTelemetryEnvelope,
    ingest_hil_telemetry_envelope,
)
from src.runtime.hil_telemetry_evidence import (
    build_hil_telemetry_evidence,
)
from src.runtime.hil_telemetry_review import (
    build_hil_telemetry_review,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.toy_grid_world import (
    ToyGridWorldAutonomyEpisodeReview,
    ToyGridWorldAutonomyScorecard,
    ToyGridWorldAutonomyScorecardStatus,
    ToyGridWorldAutonomousEpisodeStatus,
    build_toy_grid_world_autonomy_gate_result,
)


PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION = (
    "px4_gazebo_sanitized_telemetry.v1"
)


_FORBIDDEN_COMMAND_LIKE_KEYS: frozenset[str] = frozenset(
    {
        "action",
        "actions",
        "actuator",
        "actuators",
        "attitude_setpoint",
        "command",
        "commands",
        "dispatch",
        "joint",
        "live_execution_allowed",
        "mavlink_command",
        "mission_upload",
        "physical_execution_invoked",
        "position_setpoint",
        "ros_action",
        "ros_topic",
        "setpoint",
        "thrust",
        "torque",
        "velocity_command",
    }
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_FORBIDDEN_COMMAND_LIKE_KEYS_NORMALIZED: frozenset[str] = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_COMMAND_LIKE_KEYS
)


class Px4GazeboTelemetryRejected(ValueError):
    """Raised when PX4/Gazebo telemetry fails the read-only sanitizer."""


class Px4GazeboTelemetryEvidenceError(RuntimeError):
    """Raised when PX4/Gazebo telemetry cannot become HIL evidence."""


class Px4GazeboSanitizedTelemetry(BaseModel):
    """Command-free telemetry artifact derived from a PX4/Gazebo-style sample."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION] = (
        PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION
    )
    telemetry_id: str
    source_kind: str
    source_id: str
    vehicle_id: str
    captured_at: datetime
    measurements: dict[str, float | int | bool | str] = Field(default_factory=dict)
    measurement_keys: list[str] = Field(default_factory=list)
    sample_ref: str | None = None
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    command_payload_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _command_like_keys(value: Any, *, _path: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if (
                isinstance(key, str)
                and _normalize_key(key) in _FORBIDDEN_COMMAND_LIKE_KEYS_NORMALIZED
            ):
                findings.append(f"{_path}{key}" if _path else key)
            sub_path = f"{_path}{key}." if _path else f"{key}."
            findings.extend(_command_like_keys(sub, _path=sub_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            sub_path = f"{_path}{index}." if _path else f"{index}."
            findings.extend(_command_like_keys(item, _path=sub_path))
    return findings


def _string_field(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Px4GazeboTelemetryRejected(f"{field_name} must be a non-empty string")
    return value.strip()


def _parse_captured_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise Px4GazeboTelemetryRejected("captured_at must be an ISO timestamp string")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise Px4GazeboTelemetryRejected(
            f"captured_at must be a valid ISO timestamp: {value!r}"
        ) from exc


def _scalar_measurements(value: Any) -> dict[str, float | int | bool | str]:
    if not isinstance(value, dict) or not value:
        raise Px4GazeboTelemetryRejected("telemetry must be a non-empty object")

    measurements: dict[str, float | int | bool | str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise Px4GazeboTelemetryRejected("telemetry keys must be non-empty strings")
        if not isinstance(item, float | int | bool | str):
            raise Px4GazeboTelemetryRejected(
                f"telemetry.{key} must be scalar, got {type(item).__name__}"
            )
        measurements[key.strip()] = item
    return measurements


def sanitize_px4_gazebo_telemetry_sample(
    sample: dict[str, Any],
) -> Px4GazeboSanitizedTelemetry:
    """Convert one PX4/Gazebo-style telemetry sample into a safe artifact."""

    if not isinstance(sample, dict):
        raise Px4GazeboTelemetryRejected(
            f"PX4/Gazebo telemetry sample must be a dict; got {type(sample).__name__}"
        )

    forbidden = _command_like_keys(sample)
    if forbidden:
        raise Px4GazeboTelemetryRejected(
            "PX4/Gazebo telemetry sample refused command-like keys: "
            + ", ".join(sorted(forbidden))
        )

    source = sample.get("source")
    if not isinstance(source, dict):
        raise Px4GazeboTelemetryRejected("source must be an object")

    source_kind = _string_field(source.get("source_kind"), field_name="source_kind")
    source_id = _string_field(source.get("source_id"), field_name="source_id")
    vehicle_id = _string_field(source.get("vehicle_id"), field_name="vehicle_id")
    captured_at = _parse_captured_at(sample.get("captured_at"))
    measurements = _scalar_measurements(sample.get("telemetry"))
    sample_ref = sample.get("sample_id")
    if sample_ref is not None and not isinstance(sample_ref, str):
        raise Px4GazeboTelemetryRejected("sample_id must be a string when present")

    measurement_keys = sorted(measurements)
    telemetry_id = _stable_id(
        "px4_gazebo_telemetry",
        {
            "source_kind": source_kind,
            "source_id": source_id,
            "vehicle_id": vehicle_id,
            "captured_at": captured_at.isoformat(),
            "measurements": measurements,
        },
    )

    metadata = sample.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise Px4GazeboTelemetryRejected("metadata must be an object when present")

    try:
        return Px4GazeboSanitizedTelemetry(
            telemetry_id=telemetry_id,
            source_kind=source_kind,
            source_id=source_id,
            vehicle_id=vehicle_id,
            captured_at=captured_at,
            measurements=measurements,
            measurement_keys=measurement_keys,
            sample_ref=sample_ref,
            metadata={
                "artifact_only": True,
                "telemetry_only": True,
                "read_only": True,
                "source": "px4_gazebo_sample",
                "sanitizer": "px4_gazebo_telemetry.v1",
                **(metadata or {}),
            },
        )
    except ValidationError as exc:
        raise Px4GazeboTelemetryRejected(
            f"invalid sanitized PX4/Gazebo telemetry artifact: {exc}"
        ) from exc


def _as_sanitized_telemetry(
    telemetry: Px4GazeboSanitizedTelemetry | dict[str, Any],
) -> Px4GazeboSanitizedTelemetry:
    if isinstance(telemetry, Px4GazeboSanitizedTelemetry):
        normalized = telemetry
    else:
        try:
            normalized = Px4GazeboSanitizedTelemetry.model_validate(telemetry)
        except ValidationError as exc:
            raise Px4GazeboTelemetryEvidenceError(
                f"invalid PX4/Gazebo sanitized telemetry: {exc}"
            ) from exc
    forbidden = _command_like_keys(
        {
            "measurements": normalized.measurements,
            "metadata": normalized.metadata,
        }
    )
    if forbidden:
        raise Px4GazeboTelemetryRejected(
            "PX4/Gazebo sanitized telemetry refused command-like keys: "
            + ", ".join(sorted(forbidden))
        )
    return normalized


def build_px4_gazebo_hil_telemetry_contract(
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | dict[str, Any],
) -> HilTelemetryContract:
    """Build the read-only HIL contract for one sanitized PX4/Gazebo source."""

    telemetry = _as_sanitized_telemetry(sanitized_telemetry)
    contract_id = _stable_id(
        "px4_gazebo_hil_contract",
        {
            "source_kind": telemetry.source_kind,
            "source_id": telemetry.source_id,
        },
    )
    return HilTelemetryContract(
        schema_version=HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION,
        contract_id=contract_id,
        subject_kind="px4_gazebo_vehicle",
        telemetry_envelope_schema=HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    )


def build_px4_gazebo_hil_telemetry_envelope(
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | dict[str, Any],
    *,
    hil_telemetry_contract: HilTelemetryContract | dict[str, Any] | None = None,
) -> HilTelemetryEnvelope:
    """Build and ingest a HIL envelope from sanitized PX4/Gazebo telemetry."""

    telemetry = _as_sanitized_telemetry(sanitized_telemetry)
    contract = (
        HilTelemetryContract.model_validate(hil_telemetry_contract)
        if hil_telemetry_contract is not None
        else build_px4_gazebo_hil_telemetry_contract(telemetry)
    )
    if contract.subject_kind != "px4_gazebo_vehicle":
        raise Px4GazeboTelemetryEvidenceError(
            "PX4/Gazebo HIL contract subject_kind must be px4_gazebo_vehicle"
        )
    if contract.telemetry_envelope_schema != HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION:
        raise Px4GazeboTelemetryEvidenceError(
            "PX4/Gazebo HIL contract envelope schema mismatch"
        )

    payload = {
        "schema_version": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        "contract_id": contract.contract_id,
        "subject_kind": contract.subject_kind,
        "subject_id": telemetry.vehicle_id,
        "captured_at": telemetry.captured_at,
        "measurements": dict(telemetry.measurements),
        "metadata": {
            "source": "px4_gazebo_sanitized_telemetry",
            "source_kind": telemetry.source_kind,
            "source_id": telemetry.source_id,
            "sample_ref": telemetry.sample_ref,
            "sanitized_schema_version": telemetry.schema_version,
            "sanitized_telemetry_id": telemetry.telemetry_id,
            "source_metadata": telemetry.metadata,
        },
    }
    return ingest_hil_telemetry_envelope(payload)


def build_px4_gazebo_hil_telemetry_evidence(
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | dict[str, Any],
    *,
    freshness_threshold_seconds: float = 60.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the PX4/Gazebo sanitized telemetry -> HIL evidence artifact chain."""

    telemetry = _as_sanitized_telemetry(sanitized_telemetry)
    contract = build_px4_gazebo_hil_telemetry_contract(telemetry)
    envelope = build_px4_gazebo_hil_telemetry_envelope(
        telemetry,
        hil_telemetry_contract=contract,
    )
    evidence = build_hil_telemetry_evidence(
        envelope,
        hil_telemetry_contract=contract,
        freshness_threshold_seconds=freshness_threshold_seconds,
        now=now,
        metadata={
            "source_artifact": "px4_gazebo_sanitized_telemetry",
            "sanitized_telemetry_id": telemetry.telemetry_id,
            "source_kind": telemetry.source_kind,
            "source_id": telemetry.source_id,
            "source_metadata": telemetry.metadata,
        },
    )
    return {
        "px4_gazebo_sanitized_telemetry": telemetry.model_dump(mode="json"),
        "hil_telemetry_contract": contract.model_dump(mode="json"),
        "hil_telemetry_envelope": envelope.model_dump(mode="json"),
        "hil_telemetry_evidence": evidence.model_dump(mode="json"),
    }


def _px4_gazebo_gate_scorecard(
    *,
    subject_id: str,
    now: datetime | None,
) -> ToyGridWorldAutonomyScorecard:
    created_at = now or datetime.now(timezone.utc)
    return ToyGridWorldAutonomyScorecard(
        scorecard_id=_stable_id(
            "px4_gazebo_hil_scorecard",
            {"subject_id": subject_id, "created_at": created_at.isoformat()},
        ),
        episode_id=subject_id,
        plan_id="px4-gazebo-telemetry-only",
        world_id="px4-gazebo-telemetry-only",
        status=ToyGridWorldAutonomyScorecardStatus.PASSED,
        passed=True,
        goal_reached=True,
        path_efficiency=1.0,
        accepted_step_count=0,
        total_step_count=0,
        source_refs=[f"px4_gazebo_vehicle:{subject_id}"],
        created_at=created_at,
        metadata={
            "artifact_only": True,
            "telemetry_only": True,
            "source": "px4_gazebo_hil_review_gate_smoke",
            "simulator": "px4_gazebo",
        },
    )


def _px4_gazebo_gate_review(
    *,
    scorecard: ToyGridWorldAutonomyScorecard,
    now: datetime | None,
) -> ToyGridWorldAutonomyEpisodeReview:
    created_at = now or datetime.now(timezone.utc)
    return ToyGridWorldAutonomyEpisodeReview(
        review_id=_stable_id(
            "px4_gazebo_hil_episode_review",
            {
                "scorecard_id": scorecard.scorecard_id,
                "created_at": created_at.isoformat(),
            },
        ),
        episode_id=scorecard.episode_id,
        plan_id=scorecard.plan_id,
        world_id=scorecard.world_id,
        final_status=ToyGridWorldAutonomousEpisodeStatus.GOAL_REACHED.value,
        summary="PX4/Gazebo telemetry-only HIL review gate smoke.",
        scorecard_snapshot=scorecard.model_dump(mode="json"),
        source_refs=list(scorecard.source_refs),
        created_at=created_at,
        metadata={
            "artifact_only": True,
            "telemetry_only": True,
            "source": "px4_gazebo_hil_review_gate_smoke",
            "simulator": "px4_gazebo",
        },
    )


def build_px4_gazebo_hil_review_gate_smoke(
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | dict[str, Any],
    *,
    freshness_threshold_seconds: float = 60.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build PX4/Gazebo telemetry evidence, HIL review, and autonomy gate.

    This is an offline smoke helper. It does not start PX4/Gazebo, dispatch
    commands, create approvals, promote artifacts, or enable runtime reuse.
    """

    current_time = now or datetime.now(timezone.utc)
    artifacts = build_px4_gazebo_hil_telemetry_evidence(
        sanitized_telemetry,
        freshness_threshold_seconds=freshness_threshold_seconds,
        now=current_time,
    )
    hil_review = build_hil_telemetry_review(
        telemetry_evidences=[artifacts["hil_telemetry_evidence"]],
        required=True,
        now=current_time,
        metadata={
            "artifact_only": True,
            "telemetry_only": True,
            "source": "px4_gazebo_hil_evidence",
        },
    )
    evidence = artifacts["hil_telemetry_evidence"]
    scorecard = _px4_gazebo_gate_scorecard(
        subject_id=str(evidence["subject_id"]),
        now=current_time,
    )
    review = _px4_gazebo_gate_review(scorecard=scorecard, now=current_time)
    gate = build_toy_grid_world_autonomy_gate_result(
        scorecard,
        autonomy_episode_review=review,
        hil_telemetry_reviews=[hil_review],
        required_hil_telemetry_review=True,
        subject_id=str(evidence["subject_id"]),
        now=current_time,
        metadata={
            "source": "px4_gazebo_hil_review_gate_smoke",
            "telemetry_only": True,
            "px4_gazebo_sanitized_telemetry_id": artifacts[
                "px4_gazebo_sanitized_telemetry"
            ]["telemetry_id"],
        },
    )
    return {
        **artifacts,
        "hil_telemetry_review": hil_review.model_dump(mode="json"),
        "autonomy_gate_result": gate.model_dump(mode="json"),
    }


def attach_px4_gazebo_hil_review_gate_artifacts(
    task_id: str,
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | dict[str, Any],
    *,
    freshness_threshold_seconds: float = 60.0,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach PX4/Gazebo HIL evidence, review, and gate artifacts."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise Px4GazeboTelemetryEvidenceError(
            f"task {task_id} not found; cannot attach PX4/Gazebo HIL gate smoke"
        )

    artifacts = build_px4_gazebo_hil_review_gate_smoke(
        sanitized_telemetry,
        freshness_threshold_seconds=freshness_threshold_seconds,
        now=now,
    )
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise Px4GazeboTelemetryEvidenceError(
            f"task {task_id} disappeared while attaching PX4/Gazebo HIL gate smoke"
        )
    return artifacts


def attach_px4_gazebo_hil_telemetry_artifacts(
    task_id: str,
    sanitized_telemetry: Px4GazeboSanitizedTelemetry | dict[str, Any],
    *,
    freshness_threshold_seconds: float = 60.0,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach PX4/Gazebo sanitized telemetry and derived HIL evidence.

    Invalid telemetry raises before any task update, so rejected payloads are
    not persisted. Successful attachment only merges artifacts and never
    changes task status, approvals, promotion, runtime reuse, or execution
    permissions.
    """

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise Px4GazeboTelemetryEvidenceError(
            f"task {task_id} not found; cannot attach PX4/Gazebo HIL evidence"
        )

    artifacts = build_px4_gazebo_hil_telemetry_evidence(
        sanitized_telemetry,
        freshness_threshold_seconds=freshness_threshold_seconds,
        now=now,
    )
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise Px4GazeboTelemetryEvidenceError(
            f"task {task_id} disappeared while attaching PX4/Gazebo HIL evidence"
        )
    return artifacts


__all__ = [
    "PX4_GAZEBO_SANITIZED_TELEMETRY_SCHEMA_VERSION",
    "Px4GazeboSanitizedTelemetry",
    "Px4GazeboTelemetryEvidenceError",
    "Px4GazeboTelemetryRejected",
    "attach_px4_gazebo_hil_telemetry_artifacts",
    "attach_px4_gazebo_hil_review_gate_artifacts",
    "build_px4_gazebo_hil_telemetry_contract",
    "build_px4_gazebo_hil_telemetry_envelope",
    "build_px4_gazebo_hil_telemetry_evidence",
    "build_px4_gazebo_hil_review_gate_smoke",
    "sanitize_px4_gazebo_telemetry_sample",
]
