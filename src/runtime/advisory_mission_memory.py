"""Advisory cross-mission lesson memory artifacts.

Lessons may influence future mission design, but they are never authority for
verification, scorecards, success proof, dispatch, or hardware execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import ast
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.task_store import TaskStore, get_task_store

DELIVERY_MISSION_LESSON_CANDIDATE_SCHEMA_VERSION = (
    "delivery_mission_lesson_candidate.v1"
)
DELIVERY_MISSION_LESSON_PROMOTION_RECEIPT_SCHEMA_VERSION = (
    "delivery_mission_lesson_promotion_receipt.v1"
)
DELIVERY_MISSION_LESSON_SCHEMA_VERSION = "delivery_mission_lesson.v1"
VERIFIER_CONTRACT_SCHEMA_VERSION = "verifier_contract.v1"
SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION = "simulated_delivery_episode.v1"

DEFAULT_VERIFIER_PREDICATE_MODULE_PATHS = (
    "src/runtime/delivery_episode_review.py",
    "src/runtime/delivery_recovery_outcome.py",
    "src/runtime/delivery_recovery_real_sitl.py",
    "src/runtime/px4_gazebo_sitl_dropoff_verification.py",
)


class AdvisoryMissionMemoryError(RuntimeError):
    """Raised when advisory lesson artifacts violate their authority boundary."""


class LessonCandidateCreator(str, Enum):
    LLM = "llm"
    RULE = "rule"
    OPERATOR = "operator"


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(
        sorted({str(item).strip() for item in (values or ()) if str(item).strip()})
    )


def _artifact_ref_name(ref: str) -> str:
    if ":" not in ref:
        raise AdvisoryMissionMemoryError(f"invalid_artifact_ref:{ref}")
    name, artifact_id = ref.split(":", 1)
    if not name.strip() or not artifact_id.strip():
        raise AdvisoryMissionMemoryError(f"invalid_artifact_ref:{ref}")
    return name


def _resolve_task_ref(store: TaskStore, ref: str) -> dict[str, Any]:
    if not ref.startswith("task:"):
        raise AdvisoryMissionMemoryError(f"invalid_task_ref:{ref}")
    task_id = ref.split(":", 1)[1]
    task = store.get(task_id)
    if task is None:
        raise AdvisoryMissionMemoryError(f"lesson_source_mission_ref_not_found:{ref}")
    return task


def _resolve_source_tasks(
    store: TaskStore,
    source_mission_refs: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    refs = _as_tuple(source_mission_refs)
    if not refs:
        raise AdvisoryMissionMemoryError("lesson_source_mission_refs_empty")
    return tuple(_resolve_task_ref(store, ref) for ref in refs)


def _artifact_belongs_to_source_tasks(
    *,
    tasks: Sequence[Mapping[str, Any]],
    artifact_ref: str,
) -> bool:
    artifact_name = _artifact_ref_name(artifact_ref)
    for task in tasks:
        artifacts = task.get("artifacts") or {}
        if artifact_name in artifacts:
            artifact = artifacts[artifact_name]
            artifact_id = artifact_ref.split(":", 1)[1]
            if isinstance(artifact, Mapping):
                if artifact_id in {str(value) for value in artifact.values()}:
                    return True
                for key in (
                    "id",
                    "candidate_id",
                    "lesson_id",
                    "scorecard_id",
                    "review_id",
                    "outcome_id",
                    "episode_id",
                    "result_id",
                    "verification_id",
                    "recovery_run_id",
                    "loop_id",
                    "fault_event_id",
                    "request_id",
                ):
                    if str(artifact.get(key, "")) == artifact_id:
                        return True
            if artifact_id == artifact_name:
                return True
    return False


def _validate_source_refs(
    *,
    store: TaskStore,
    source_mission_refs: Sequence[str],
    source_artifact_refs: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    mission_refs = _as_tuple(source_mission_refs)
    artifact_refs = _as_tuple(source_artifact_refs)
    tasks = _resolve_source_tasks(store, mission_refs)
    if not artifact_refs:
        raise AdvisoryMissionMemoryError("lesson_source_artifact_refs_empty")
    for task in tasks:
        if task.get("status") != "completed":
            raise AdvisoryMissionMemoryError(
                f"lesson_source_mission_not_finalized:task:{task.get('task_id')}"
            )
    for ref in artifact_refs:
        if not _artifact_belongs_to_source_tasks(tasks=tasks, artifact_ref=ref):
            raise AdvisoryMissionMemoryError(
                f"lesson_source_artifact_ref_not_found:{ref}"
            )
    return mission_refs, artifact_refs


def _task_artifact_ref(name: str, artifact: Mapping[str, Any]) -> str:
    for key in (
        "candidate_id",
        "promotion_receipt_id",
        "lesson_id",
        "contract_id",
    ):
        value = artifact.get(key)
        if value:
            return f"{name}:{value}"
    raise AdvisoryMissionMemoryError(f"artifact_missing_ref_id:{name}")


def _find_artifact_by_ref(
    task: Mapping[str, Any],
    ref: str,
) -> tuple[str, Mapping[str, Any]]:
    name = _artifact_ref_name(ref)
    artifact = (task.get("artifacts") or {}).get(name)
    if not isinstance(artifact, Mapping):
        raise AdvisoryMissionMemoryError(f"artifact_ref_not_found:{ref}")
    return name, artifact


def _artifact_ref_id(ref: str) -> str:
    _artifact_ref_name(ref)
    return ref.split(":", 1)[1]


def _artifact_matches_ref(name: str, artifact: Mapping[str, Any], ref: str) -> bool:
    try:
        return _task_artifact_ref(name, artifact) == ref
    except AdvisoryMissionMemoryError:
        return False


def _find_exact_artifact_by_ref(
    task: Mapping[str, Any],
    ref: str,
) -> tuple[str, Mapping[str, Any]]:
    name = _artifact_ref_name(ref)
    artifact = (task.get("artifacts") or {}).get(name)
    if isinstance(artifact, Mapping) and _artifact_matches_ref(name, artifact, ref):
        return name, artifact
    for artifact in (task.get("artifacts") or {}).values():
        if isinstance(artifact, Mapping) and _artifact_matches_ref(name, artifact, ref):
            return name, artifact
    raise AdvisoryMissionMemoryError(f"artifact_ref_not_found:{ref}")


def _find_lesson_payload_by_ref(
    task: Mapping[str, Any],
    ref: str,
) -> Mapping[str, Any]:
    name = _artifact_ref_name(ref)
    if name != "delivery_mission_lesson":
        raise AdvisoryMissionMemoryError(f"lesson_ref_invalid:{ref}")
    lesson_id = _artifact_ref_id(ref)
    direct = (task.get("artifacts") or {}).get(name)
    if isinstance(direct, Mapping) and direct.get("lesson_id") == lesson_id:
        return direct
    for artifact in (task.get("artifacts") or {}).values():
        if not isinstance(artifact, Mapping):
            continue
        if artifact.get("schema_version") != DELIVERY_MISSION_LESSON_SCHEMA_VERSION:
            continue
        if artifact.get("lesson_id") == lesson_id:
            return artifact
    raise AdvisoryMissionMemoryError(f"lesson_supersession_target_not_found:{ref}")


def _iter_mapping_values(value: Any) -> Sequence[Any]:
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(value)
    return ()


def _contains_ref(value: Any, ref: str) -> bool:
    if isinstance(value, str):
        return value == ref
    if isinstance(value, Mapping):
        return any(_contains_ref(item, ref) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_ref(item, ref) for item in value)
    return False


def _iter_task_chain(
    *,
    task: Mapping[str, Any],
    source_mission_refs: Sequence[str],
    task_store_factory: Callable[[], TaskStore] | None,
) -> tuple[Mapping[str, Any], ...]:
    store = (task_store_factory or get_task_store)()
    tasks: list[Mapping[str, Any]] = [task]
    for ref in _as_tuple(source_mission_refs):
        tasks.append(_resolve_task_ref(store, ref))
    return tuple(tasks)


def _receipt_artifacts_for_candidate(
    task: Mapping[str, Any],
    lesson_candidate_ref: str,
) -> tuple[Mapping[str, Any], ...]:
    receipts: list[Mapping[str, Any]] = []
    for name, artifact in (task.get("artifacts") or {}).items():
        if not isinstance(artifact, Mapping):
            continue
        if artifact.get("schema_version") != (
            DELIVERY_MISSION_LESSON_PROMOTION_RECEIPT_SCHEMA_VERSION
        ) and name != "delivery_mission_lesson_promotion_receipt":
            continue
        if artifact.get("lesson_candidate_ref") == lesson_candidate_ref:
            receipts.append(artifact)
    return tuple(receipts)


def _raise_if_lesson_used_as_authority(
    *,
    tasks: Sequence[Mapping[str, Any]],
    lesson_ref: str,
) -> None:
    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            if any("scorecard" in item for item in path) and _contains_ref(
                value.get("evidence_refs"),
                lesson_ref,
            ):
                raise AdvisoryMissionMemoryError("lesson_used_as_scorecard_evidence")
            if path and path[-1] == "scorecard" and _contains_ref(
                value.get("evidence_refs"), lesson_ref
            ):
                raise AdvisoryMissionMemoryError("lesson_used_as_scorecard_evidence")
            for key, item in value.items():
                key_text = str(key)
                if (
                    key_text in {"verifier_input_refs", "verifier_inputs"}
                    or key_text.endswith("_verifier_input_refs")
                ) and _contains_ref(item, lesson_ref):
                    raise AdvisoryMissionMemoryError("lesson_used_as_verifier_input")
                walk(item, (*path, key_text))
            return
        for item in _iter_mapping_values(value):
            walk(item, path)

    for item in tasks:
        walk(item.get("artifacts") or {}, ())


class LessonApplicability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vehicle_class: str | None = None
    payload_kg_min: float | None = Field(default=None, ge=0)
    payload_kg_max: float | None = Field(default=None, ge=0)
    wind_mps_min: float | None = Field(default=None, ge=0)
    wind_mps_max: float | None = Field(default=None, ge=0)
    altitude_m_min: float | None = None
    altitude_m_max: float | None = None
    terrain_class: str | None = None
    mission_profile: str | None = None

    @model_validator(mode="after")
    def _validate_ranges(self) -> "LessonApplicability":
        for prefix in ("payload_kg", "wind_mps", "altitude_m"):
            low = getattr(self, f"{prefix}_min")
            high = getattr(self, f"{prefix}_max")
            if low is not None and high is not None and low > high:
                raise AdvisoryMissionMemoryError(
                    f"lesson_applicability_invalid_range:{prefix}"
                )
        return self

    @property
    def has_constraints(self) -> bool:
        return any(value is not None for value in self.model_dump().values())


class LessonRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_summary: str
    design_hint: str
    avoid_scenario_summary: str | None = None

    @model_validator(mode="after")
    def _validate_recommendation(self) -> "LessonRecommendation":
        if not self.recommendation_summary.strip():
            raise AdvisoryMissionMemoryError("lesson_recommendation_empty")
        if not self.design_hint.strip():
            raise AdvisoryMissionMemoryError("lesson_design_hint_empty")
        raise_for_command_like_payload(
            self.model_dump(mode="json"),
            root="lesson_recommendation",
            error_type=AdvisoryMissionMemoryError,
            prefix="lesson recommendation refused command-like payload",
        )
        return self


class MissionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vehicle_class: str | None = None
    payload_kg: float | None = Field(default=None, ge=0)
    wind_mps: float | None = Field(default=None, ge=0)
    altitude_m: float | None = None
    terrain_class: str | None = None
    mission_profile: str | None = None


class DeliveryMissionLessonCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_MISSION_LESSON_CANDIDATE_SCHEMA_VERSION] = (
        DELIVERY_MISSION_LESSON_CANDIDATE_SCHEMA_VERSION
    )
    candidate_id: str
    source_mission_refs: tuple[str, ...]
    source_artifact_refs: tuple[str, ...]
    proposed_recommendation: LessonRecommendation
    proposed_applicability: LessonApplicability
    rationale: str
    created_by: LessonCandidateCreator
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    advisory_only: Literal[True] = True
    is_promoted: Literal[False] = False
    usable_in_scenario_design: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    external_dispatch_performed: Literal[False] = False
    verifier_predicate_change_proposed: Literal[False] = False
    used_as_scorecard_evidence: Literal[False] = False

    @field_validator("source_mission_refs", "source_artifact_refs", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_candidate(self) -> "DeliveryMissionLessonCandidate":
        raise_for_command_like_payload(
            self.metadata,
            root="lesson_candidate.metadata",
            error_type=AdvisoryMissionMemoryError,
            prefix="lesson candidate refused command-like metadata",
        )
        if not self.source_mission_refs:
            raise AdvisoryMissionMemoryError("lesson_source_mission_refs_empty")
        if not self.source_artifact_refs:
            raise AdvisoryMissionMemoryError("lesson_source_artifact_refs_empty")
        if not self.rationale.strip():
            raise AdvisoryMissionMemoryError("lesson_candidate_rationale_empty")
        return self


class DeliveryMissionLessonPromotionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_MISSION_LESSON_PROMOTION_RECEIPT_SCHEMA_VERSION] = (
        DELIVERY_MISSION_LESSON_PROMOTION_RECEIPT_SCHEMA_VERSION
    )
    promotion_receipt_id: str
    lesson_candidate_ref: str
    operator_id: str
    operator_decision: Literal["promote"] = "promote"
    decision_at: datetime
    decision_rationale: str
    auto_promotion_used: Literal[False] = False
    llm_decided_promotion: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False

    @field_validator("decision_at", mode="before")
    @classmethod
    def _coerce_decision_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_receipt(self) -> "DeliveryMissionLessonPromotionReceipt":
        if not self.operator_id.strip():
            raise AdvisoryMissionMemoryError("lesson_promotion_operator_id_empty")
        if not self.decision_rationale.strip():
            raise AdvisoryMissionMemoryError("lesson_promotion_rationale_empty")
        _artifact_ref_name(self.lesson_candidate_ref)
        return self


class DeliveryMissionLesson(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DELIVERY_MISSION_LESSON_SCHEMA_VERSION] = (
        DELIVERY_MISSION_LESSON_SCHEMA_VERSION
    )
    lesson_id: str
    lesson_candidate_ref: str
    operator_promotion_receipt_ref: str
    source_mission_refs: tuple[str, ...]
    source_artifact_refs: tuple[str, ...]
    recommendation: LessonRecommendation
    applicability: LessonApplicability
    rationale: str
    created_at: datetime
    valid_for_episode_schema_versions: tuple[
        Literal[SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION], ...
    ]
    superseded_by_lesson_ref: str | None = None
    expired_at: datetime | None = None
    superseding_promotion_receipt_ref: str | None = None
    warning_reasons: tuple[str, ...] = ()
    advisory_only: Literal[True] = True
    usable_in_scenario_design: Literal[True] = True
    usable_as_scorecard_evidence: Literal[False] = False
    usable_as_verifier_input: Literal[False] = False
    usable_as_success_proof: Literal[False] = False
    verifier_predicate_change_proposed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    external_dispatch_performed: Literal[False] = False

    @field_validator(
        "source_mission_refs",
        "source_artifact_refs",
        "valid_for_episode_schema_versions",
        "warning_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("created_at", "expired_at", mode="before")
    @classmethod
    def _coerce_optional_datetime(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_lesson(self) -> "DeliveryMissionLesson":
        if not self.operator_promotion_receipt_ref.strip():
            raise AdvisoryMissionMemoryError("lesson_operator_promotion_receipt_missing")
        if not self.source_mission_refs:
            raise AdvisoryMissionMemoryError("lesson_source_mission_refs_empty")
        if not self.source_artifact_refs:
            raise AdvisoryMissionMemoryError("lesson_source_artifact_refs_empty")
        if not self.valid_for_episode_schema_versions:
            raise AdvisoryMissionMemoryError("lesson_valid_episode_versions_empty")
        if self.superseded_by_lesson_ref and not self.superseding_promotion_receipt_ref:
            raise AdvisoryMissionMemoryError(
                "lesson_superseding_promotion_receipt_missing"
            )
        if not self.rationale.strip():
            raise AdvisoryMissionMemoryError("lesson_rationale_empty")
        return self


class VerifierContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[VERIFIER_CONTRACT_SCHEMA_VERSION] = (
        VERIFIER_CONTRACT_SCHEMA_VERSION
    )
    contract_id: str
    predicate_module_paths: tuple[str, ...]
    predicate_source_hash_algorithm: Literal["sha256"] = "sha256"
    canonicalization_mode: Literal["py_source_normalized_v1"] = (
        "py_source_normalized_v1"
    )
    created_at: datetime
    mutable: Literal[False] = False
    lesson_influenced: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False

    @field_validator("predicate_module_paths", mode="before")
    @classmethod
    def _coerce_paths(cls, value: Any) -> tuple[str, ...]:
        return _as_tuple(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_contract(self) -> "VerifierContract":
        if not self.contract_id.startswith("verifier_contract_"):
            raise AdvisoryMissionMemoryError("verifier_contract_id_prefix_invalid")
        if not self.predicate_module_paths:
            raise AdvisoryMissionMemoryError("verifier_contract_paths_empty")
        return self


def canonicalize_python_source(source: str) -> str:
    tree = ast.parse(source)
    normalized = ast.dump(tree, annotate_fields=True, include_attributes=False)
    return normalized + "\n"


def verifier_contract_digest_for_paths(
    predicate_module_paths: Sequence[str],
    *,
    root: Path | None = None,
) -> str:
    base = root or Path.cwd()
    chunks: list[str] = []
    for item in _as_tuple(predicate_module_paths):
        path = (base / item).resolve()
        if not path.exists():
            raise AdvisoryMissionMemoryError(f"verifier_contract_path_missing:{item}")
        chunks.append(f"## {item}\n{canonicalize_python_source(path.read_text())}")
    return sha256("\n".join(chunks).encode("utf-8")).hexdigest()


def current_verifier_contract(
    *,
    predicate_module_paths: Sequence[str] | None = None,
    root: Path | None = None,
    created_at: datetime | None = None,
) -> VerifierContract:
    paths = _as_tuple(predicate_module_paths or DEFAULT_VERIFIER_PREDICATE_MODULE_PATHS)
    digest = verifier_contract_digest_for_paths(paths, root=root)
    return VerifierContract(
        contract_id=f"verifier_contract_{digest[:16]}",
        predicate_module_paths=paths,
        created_at=_utc(created_at),
    )


def build_verifier_contract(
    *,
    predicate_module_paths: Sequence[str],
    contract_id: str | None = None,
    root: Path | None = None,
    created_at: datetime | None = None,
) -> VerifierContract:
    paths = _as_tuple(predicate_module_paths)
    digest = verifier_contract_digest_for_paths(paths, root=root)
    resolved_id = contract_id or f"verifier_contract_{digest[:16]}"
    expected_id = f"verifier_contract_{digest[:16]}"
    if resolved_id != expected_id:
        raise AdvisoryMissionMemoryError("verifier_contract_id_hash_mismatch")
    return VerifierContract(
        contract_id=resolved_id,
        predicate_module_paths=paths,
        created_at=_utc(created_at),
    )


def build_delivery_mission_lesson_candidate(
    *,
    source_mission_refs: Sequence[str],
    source_artifact_refs: Sequence[str],
    proposed_recommendation: LessonRecommendation | Mapping[str, Any],
    proposed_applicability: LessonApplicability | Mapping[str, Any],
    rationale: str,
    created_by: LessonCandidateCreator | str,
    created_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> DeliveryMissionLessonCandidate:
    store = (task_store_factory or get_task_store)()
    mission_refs, artifact_refs = _validate_source_refs(
        store=store,
        source_mission_refs=source_mission_refs,
        source_artifact_refs=source_artifact_refs,
    )
    recommendation = (
        proposed_recommendation
        if isinstance(proposed_recommendation, LessonRecommendation)
        else LessonRecommendation.model_validate(dict(proposed_recommendation))
    )
    applicability = (
        proposed_applicability
        if isinstance(proposed_applicability, LessonApplicability)
        else LessonApplicability.model_validate(dict(proposed_applicability))
    )
    created = _utc(created_at)
    metadata_payload = dict(metadata or {})
    payload = {
        "source_mission_refs": mission_refs,
        "source_artifact_refs": artifact_refs,
        "recommendation": recommendation.model_dump(mode="json"),
        "applicability": applicability.model_dump(mode="json"),
        "rationale": rationale,
        "created_by": str(created_by.value if isinstance(created_by, Enum) else created_by),
        "created_at": created.isoformat(),
    }
    return DeliveryMissionLessonCandidate(
        candidate_id=_stable_id("delivery_mission_lesson_candidate", payload),
        source_mission_refs=mission_refs,
        source_artifact_refs=artifact_refs,
        proposed_recommendation=recommendation,
        proposed_applicability=applicability,
        rationale=rationale,
        created_by=(
            created_by
            if isinstance(created_by, LessonCandidateCreator)
            else LessonCandidateCreator(str(created_by))
        ),
        created_at=created,
        metadata=metadata_payload,
    )


def attach_delivery_mission_lesson_candidate(
    task_id: str,
    *,
    source_mission_refs: Sequence[str],
    source_artifact_refs: Sequence[str],
    proposed_recommendation: LessonRecommendation | Mapping[str, Any],
    proposed_applicability: LessonApplicability | Mapping[str, Any],
    rationale: str,
    created_by: LessonCandidateCreator | str,
    created_at: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    if store.get(task_id) is None:
        raise AdvisoryMissionMemoryError(
            f"task {task_id} not found; cannot attach lesson candidate"
        )
    candidate = build_delivery_mission_lesson_candidate(
        source_mission_refs=source_mission_refs,
        source_artifact_refs=source_artifact_refs,
        proposed_recommendation=proposed_recommendation,
        proposed_applicability=proposed_applicability,
        rationale=rationale,
        created_by=created_by,
        created_at=created_at,
        metadata=metadata,
        task_store_factory=lambda: store,
    )
    artifacts = {
        "delivery_mission_lesson_candidate": candidate.model_dump(mode="json")
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise AdvisoryMissionMemoryError(
            f"task {task_id} disappeared while attaching lesson candidate"
        )
    return {**artifacts, "task": updated}


def build_delivery_mission_lesson_promotion_receipt(
    *,
    task: Mapping[str, Any],
    lesson_candidate_ref: str,
    operator_id: str,
    decision_rationale: str,
    decision_at: datetime | None = None,
) -> DeliveryMissionLessonPromotionReceipt:
    _find_artifact_by_ref(task, lesson_candidate_ref)
    for name, artifact in (task.get("artifacts") or {}).items():
        if name == "delivery_mission_lesson_promotion_receipt" and isinstance(
            artifact, Mapping
        ):
            if artifact.get("lesson_candidate_ref") == lesson_candidate_ref:
                raise AdvisoryMissionMemoryError(
                    "lesson_candidate_already_promoted"
                )
    decided = _utc(decision_at)
    payload = {
        "candidate": lesson_candidate_ref,
        "operator_id": operator_id,
        "decision_at": decided.isoformat(),
        "decision_rationale": decision_rationale,
        "auto_promotion_used": False,
        "llm_decided_promotion": False,
    }
    return DeliveryMissionLessonPromotionReceipt(
        promotion_receipt_id=_stable_id(
            "delivery_mission_lesson_promotion_receipt", payload
        ),
        lesson_candidate_ref=lesson_candidate_ref,
        operator_id=operator_id,
        decision_at=decided,
        decision_rationale=decision_rationale,
    )


def build_delivery_mission_lesson(
    *,
    task: Mapping[str, Any],
    lesson_candidate_ref: str,
    operator_promotion_receipt_ref: str,
    created_at: datetime | None = None,
    valid_for_episode_schema_versions: Sequence[str] | None = None,
    superseded_by_lesson_ref: str | None = None,
    expired_at: datetime | None = None,
    superseding_promotion_receipt_ref: str | None = None,
) -> DeliveryMissionLesson:
    _, candidate_payload = _find_artifact_by_ref(task, lesson_candidate_ref)
    _, receipt_payload = _find_artifact_by_ref(task, operator_promotion_receipt_ref)
    candidate = DeliveryMissionLessonCandidate.model_validate(candidate_payload)
    receipt = DeliveryMissionLessonPromotionReceipt.model_validate(receipt_payload)
    if receipt.lesson_candidate_ref != lesson_candidate_ref:
        raise AdvisoryMissionMemoryError("lesson_receipt_candidate_ref_mismatch")
    if not operator_promotion_receipt_ref.strip():
        raise AdvisoryMissionMemoryError("lesson_operator_promotion_receipt_missing")
    created = _utc(created_at)
    versions = _as_tuple(
        valid_for_episode_schema_versions or (SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,)
    )
    warning_reasons = (
        ("lesson_has_no_applicability_constraints",)
        if not candidate.proposed_applicability.has_constraints
        else ()
    )
    payload = {
        "candidate": lesson_candidate_ref,
        "receipt": operator_promotion_receipt_ref,
        "source_mission_refs": candidate.source_mission_refs,
        "source_artifact_refs": candidate.source_artifact_refs,
        "recommendation": candidate.proposed_recommendation.model_dump(mode="json"),
        "applicability": candidate.proposed_applicability.model_dump(mode="json"),
        "versions": versions,
        "created_at": created.isoformat(),
    }
    return DeliveryMissionLesson(
        lesson_id=_stable_id("delivery_mission_lesson", payload),
        lesson_candidate_ref=lesson_candidate_ref,
        operator_promotion_receipt_ref=operator_promotion_receipt_ref,
        source_mission_refs=candidate.source_mission_refs,
        source_artifact_refs=candidate.source_artifact_refs,
        recommendation=candidate.proposed_recommendation,
        applicability=candidate.proposed_applicability,
        rationale=candidate.rationale,
        created_at=created,
        valid_for_episode_schema_versions=versions,
        superseded_by_lesson_ref=superseded_by_lesson_ref,
        expired_at=expired_at,
        superseding_promotion_receipt_ref=superseding_promotion_receipt_ref,
        warning_reasons=warning_reasons,
    )


def attach_delivery_mission_lesson_promotion(
    task_id: str,
    *,
    lesson_candidate_ref: str,
    operator_id: str,
    decision_rationale: str,
    decision_at: datetime | None = None,
    created_at: datetime | None = None,
    valid_for_episode_schema_versions: Sequence[str] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    task = store.get(task_id)
    if task is None:
        raise AdvisoryMissionMemoryError(
            f"task {task_id} not found; cannot attach promoted lesson"
        )
    receipt = build_delivery_mission_lesson_promotion_receipt(
        task=task,
        lesson_candidate_ref=lesson_candidate_ref,
        operator_id=operator_id,
        decision_rationale=decision_rationale,
        decision_at=decision_at,
    )
    receipt_ref = f"delivery_mission_lesson_promotion_receipt:{receipt.promotion_receipt_id}"
    task_with_receipt = {
        **task,
        "artifacts": {
            **(task.get("artifacts") or {}),
            "delivery_mission_lesson_promotion_receipt": receipt.model_dump(mode="json"),
        },
    }
    lesson = build_delivery_mission_lesson(
        task=task_with_receipt,
        lesson_candidate_ref=lesson_candidate_ref,
        operator_promotion_receipt_ref=receipt_ref,
        created_at=created_at,
        valid_for_episode_schema_versions=valid_for_episode_schema_versions,
    )
    task_with_lesson = {
        **task_with_receipt,
        "artifacts": {
            **(task_with_receipt.get("artifacts") or {}),
            "delivery_mission_lesson": lesson.model_dump(mode="json"),
        },
    }
    validate_lesson_refs(
        task=task_with_lesson,
        lesson_candidate_ref=lesson_candidate_ref,
        promotion_receipt_ref=receipt_ref,
        lesson_ref=f"delivery_mission_lesson:{lesson.lesson_id}",
        task_store_factory=lambda: store,
    )
    artifacts = {
        "delivery_mission_lesson_promotion_receipt": receipt.model_dump(mode="json"),
        "delivery_mission_lesson": lesson.model_dump(mode="json"),
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise AdvisoryMissionMemoryError(
            f"task {task_id} disappeared while attaching promoted lesson"
        )
    return {**artifacts, "task": updated}


def lesson_applies_to(
    lesson: DeliveryMissionLesson | Mapping[str, Any],
    envelope: MissionEnvelope | Mapping[str, Any],
    *,
    episode_schema_version: str,
    now: datetime | None = None,
) -> bool:
    item = (
        lesson
        if isinstance(lesson, DeliveryMissionLesson)
        else DeliveryMissionLesson.model_validate(dict(lesson))
    )
    env = (
        envelope
        if isinstance(envelope, MissionEnvelope)
        else MissionEnvelope.model_validate(dict(envelope))
    )
    if episode_schema_version not in item.valid_for_episode_schema_versions:
        return False
    if item.superseded_by_lesson_ref:
        return False
    if item.expired_at is not None and item.expired_at <= _utc(now):
        return False
    app = item.applicability
    if app.vehicle_class is not None and app.vehicle_class != env.vehicle_class:
        return False
    if app.terrain_class is not None and app.terrain_class != env.terrain_class:
        return False
    if app.mission_profile is not None and app.mission_profile != env.mission_profile:
        return False
    bounds = (
        ("payload_kg", env.payload_kg, app.payload_kg_min, app.payload_kg_max),
        ("wind_mps", env.wind_mps, app.wind_mps_min, app.wind_mps_max),
        ("altitude_m", env.altitude_m, app.altitude_m_min, app.altitude_m_max),
    )
    for _name, value, low, high in bounds:
        if (low is not None or high is not None) and value is None:
            return False
        if low is not None and value is not None and value < low:
            return False
        if high is not None and value is not None and value > high:
            return False
    return True


def _validate_candidate_ref(
    *,
    task: Mapping[str, Any],
    lesson_candidate_ref: str,
    task_store_factory: Callable[[], TaskStore] | None,
) -> DeliveryMissionLessonCandidate:
    name, candidate_payload = _find_exact_artifact_by_ref(task, lesson_candidate_ref)
    if name != "delivery_mission_lesson_candidate":
        raise AdvisoryMissionMemoryError(
            f"lesson_candidate_ref_invalid:{lesson_candidate_ref}"
        )
    if candidate_payload.get("verifier_predicate_change_proposed") is True:
        raise AdvisoryMissionMemoryError("lesson_verifier_predicate_change_proposed")
    if candidate_payload.get("used_as_scorecard_evidence") is True:
        raise AdvisoryMissionMemoryError("lesson_used_as_scorecard_evidence")
    candidate = DeliveryMissionLessonCandidate.model_validate(candidate_payload)
    store = (task_store_factory or get_task_store)()
    _validate_source_refs(
        store=store,
        source_mission_refs=candidate.source_mission_refs,
        source_artifact_refs=candidate.source_artifact_refs,
    )
    return candidate


def _validate_promotion_receipt_ref(
    *,
    task: Mapping[str, Any],
    promotion_receipt_ref: str,
) -> DeliveryMissionLessonPromotionReceipt:
    name, receipt_payload = _find_exact_artifact_by_ref(task, promotion_receipt_ref)
    if name != "delivery_mission_lesson_promotion_receipt":
        raise AdvisoryMissionMemoryError(
            f"lesson_promotion_receipt_ref_invalid:{promotion_receipt_ref}"
        )
    if receipt_payload.get("auto_promotion_used") is True:
        raise AdvisoryMissionMemoryError("lesson_auto_promotion_used")
    if receipt_payload.get("llm_decided_promotion") is True:
        raise AdvisoryMissionMemoryError("lesson_llm_decided_promotion")
    receipt = DeliveryMissionLessonPromotionReceipt.model_validate(receipt_payload)
    _find_exact_artifact_by_ref(task, receipt.lesson_candidate_ref)
    if len(_receipt_artifacts_for_candidate(task, receipt.lesson_candidate_ref)) > 1:
        raise AdvisoryMissionMemoryError("lesson_candidate_already_promoted")
    return receipt


def _validate_lesson_ref(
    *,
    task: Mapping[str, Any],
    lesson_ref: str,
    task_store_factory: Callable[[], TaskStore] | None,
) -> DeliveryMissionLesson:
    name, lesson_payload = _find_exact_artifact_by_ref(task, lesson_ref)
    if name != "delivery_mission_lesson":
        raise AdvisoryMissionMemoryError(f"lesson_ref_invalid:{lesson_ref}")
    if lesson_payload.get("operator_promotion_receipt_ref") in {None, ""}:
        raise AdvisoryMissionMemoryError("lesson_operator_promotion_receipt_missing")
    if lesson_payload.get("verifier_predicate_change_proposed") is True:
        raise AdvisoryMissionMemoryError("lesson_verifier_predicate_change_proposed")
    if lesson_payload.get("usable_as_scorecard_evidence") is True:
        raise AdvisoryMissionMemoryError("lesson_used_as_scorecard_evidence")
    if lesson_payload.get("usable_as_verifier_input") is True:
        raise AdvisoryMissionMemoryError("lesson_used_as_verifier_input")
    lesson = DeliveryMissionLesson.model_validate(lesson_payload)
    candidate = _validate_candidate_ref(
        task=task,
        lesson_candidate_ref=lesson.lesson_candidate_ref,
        task_store_factory=task_store_factory,
    )
    receipt = _validate_promotion_receipt_ref(
        task=task,
        promotion_receipt_ref=lesson.operator_promotion_receipt_ref,
    )
    if receipt.lesson_candidate_ref != lesson.lesson_candidate_ref:
        raise AdvisoryMissionMemoryError("lesson_receipt_candidate_ref_mismatch")
    if candidate.source_mission_refs != lesson.source_mission_refs:
        raise AdvisoryMissionMemoryError("lesson_source_refs_swapped_during_promotion")
    if candidate.source_artifact_refs != lesson.source_artifact_refs:
        raise AdvisoryMissionMemoryError("lesson_source_refs_swapped_during_promotion")

    task_chain = _iter_task_chain(
        task=task,
        source_mission_refs=lesson.source_mission_refs,
        task_store_factory=task_store_factory,
    )
    _raise_if_lesson_used_as_authority(tasks=task_chain, lesson_ref=lesson_ref)

    if lesson.superseded_by_lesson_ref:
        if not lesson.superseding_promotion_receipt_ref:
            raise AdvisoryMissionMemoryError(
                "lesson_superseding_promotion_receipt_missing"
            )
        _validate_promotion_receipt_ref(
            task=task,
            promotion_receipt_ref=lesson.superseding_promotion_receipt_ref,
        )
        superseding_payload = _find_lesson_payload_by_ref(
            task,
            lesson.superseded_by_lesson_ref,
        )
        superseding = DeliveryMissionLesson.model_validate(superseding_payload)
        mission_refs = set(lesson.source_mission_refs)
        artifact_refs = set(lesson.source_artifact_refs)
        target_mission_refs = set(superseding.source_mission_refs)
        target_artifact_refs = set(superseding.source_artifact_refs)
        if not mission_refs.issubset(target_mission_refs):
            raise AdvisoryMissionMemoryError(
                "lesson_supersession_source_refs_not_strict_superset"
            )
        if not artifact_refs.issubset(target_artifact_refs):
            raise AdvisoryMissionMemoryError(
                "lesson_supersession_source_refs_not_strict_superset"
            )
        if (
            mission_refs == target_mission_refs
            and artifact_refs == target_artifact_refs
        ):
            raise AdvisoryMissionMemoryError(
                "lesson_supersession_source_refs_not_strict_superset"
            )
    return lesson


def validate_lesson_refs(
    *,
    task: Mapping[str, Any],
    lesson_candidate_ref: str | None = None,
    lesson_ref: str | None = None,
    promotion_receipt_ref: str | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> None:
    """Validate advisory lesson references without granting lesson authority."""

    receipt: DeliveryMissionLessonPromotionReceipt | None = None
    lesson: DeliveryMissionLesson | None = None

    if lesson_candidate_ref is not None:
        _validate_candidate_ref(
            task=task,
            lesson_candidate_ref=lesson_candidate_ref,
            task_store_factory=task_store_factory,
        )
    if promotion_receipt_ref is not None:
        receipt = _validate_promotion_receipt_ref(
            task=task,
            promotion_receipt_ref=promotion_receipt_ref,
        )
    if lesson_ref is not None:
        lesson = _validate_lesson_ref(
            task=task,
            lesson_ref=lesson_ref,
            task_store_factory=task_store_factory,
        )
    if receipt is not None and lesson_candidate_ref is not None:
        if receipt.lesson_candidate_ref != lesson_candidate_ref:
            raise AdvisoryMissionMemoryError(
                "lesson_validator_candidate_receipt_ref_mismatch"
            )
    if lesson is not None and lesson_candidate_ref is not None:
        if lesson.lesson_candidate_ref != lesson_candidate_ref:
            raise AdvisoryMissionMemoryError(
                "lesson_validator_candidate_lesson_ref_mismatch"
            )
    if lesson is not None and promotion_receipt_ref is not None:
        if lesson.operator_promotion_receipt_ref != promotion_receipt_ref:
            raise AdvisoryMissionMemoryError(
                "lesson_validator_receipt_lesson_ref_mismatch"
            )


__all__ = [
    "DEFAULT_VERIFIER_PREDICATE_MODULE_PATHS",
    "DELIVERY_MISSION_LESSON_CANDIDATE_SCHEMA_VERSION",
    "DELIVERY_MISSION_LESSON_PROMOTION_RECEIPT_SCHEMA_VERSION",
    "DELIVERY_MISSION_LESSON_SCHEMA_VERSION",
    "VERIFIER_CONTRACT_SCHEMA_VERSION",
    "AdvisoryMissionMemoryError",
    "DeliveryMissionLesson",
    "DeliveryMissionLessonCandidate",
    "DeliveryMissionLessonPromotionReceipt",
    "LessonApplicability",
    "LessonCandidateCreator",
    "LessonRecommendation",
    "MissionEnvelope",
    "VerifierContract",
    "attach_delivery_mission_lesson_candidate",
    "attach_delivery_mission_lesson_promotion",
    "build_delivery_mission_lesson",
    "build_delivery_mission_lesson_candidate",
    "build_delivery_mission_lesson_promotion_receipt",
    "build_verifier_contract",
    "canonicalize_python_source",
    "current_verifier_contract",
    "lesson_applies_to",
    "validate_lesson_refs",
    "verifier_contract_digest_for_paths",
]
