"""Prompt-to-scenario proposal artifacts for PX4/Gazebo mission design.

This module intentionally stops at proposal validation and artifact-level dry
run.  A prompt-derived scenario can inform an operator review surface, but it
does not grant simulator execution, MAVLink dispatch, or stronger recovery
authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.advisory_mission_memory import (
    DeliveryMissionLesson,
    MissionEnvelope,
    current_verifier_contract,
    lesson_applies_to,
)
from src.runtime.delivery_recovery_safety import raise_for_command_like_payload
from src.runtime.digital_twin_mission_environment import (
    DIGITAL_TWIN_ROUTE_FEASIBILITY_SCHEMA_VERSION,
    DIGITAL_TWIN_ROUTE_PLAN_SCHEMA_VERSION,
    REAL_WORLD_GEOCODE_CANDIDATE_SCHEMA_VERSION,
    REAL_WORLD_MISSION_TARGET_SCHEMA_VERSION,
    TERRAIN_DEM_TILE_REQUEST_CANDIDATE_SCHEMA_VERSION,
    TERRAIN_DEM_TILE_SNAPSHOT_SCHEMA_VERSION,
    TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION,
    TILE_BACKED_TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION,
    WEATHER_ENVIRONMENT_POLICY_GATE_SCHEMA_VERSION,
    WEATHER_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION,
    build_digital_twin_stage1_environment,
)

PX4_GAZEBO_MISSION_PROMPT_REQUEST_SCHEMA_VERSION = (
    "px4_gazebo_mission_prompt_request.v1"
)
PX4_GAZEBO_MISSION_SCENARIO_PROPOSAL_SCHEMA_VERSION = (
    "px4_gazebo_mission_scenario_proposal.v1"
)
PX4_GAZEBO_MISSION_SCENARIO_VALIDATION_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_mission_scenario_validation_result.v1"
)
PX4_GAZEBO_MISSION_SCENARIO_DRY_RUN_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_mission_scenario_dry_run_result.v1"
)
PX4_GAZEBO_MISSION_SCENARIO_APPROVAL_SCHEMA_VERSION = (
    "px4_gazebo_mission_scenario_approval.v1"
)
PX4_GAZEBO_MISSION_SCENARIO_COMPILE_RESULT_SCHEMA_VERSION = (
    "px4_gazebo_mission_scenario_compile_result.v1"
)
PX4_GAZEBO_BOUNDED_SIMULATION_REQUEST_SCHEMA_VERSION = (
    "px4_gazebo_bounded_simulation_request.v1"
)
PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_REQUEST_SCHEMA_VERSION = (
    "px4_gazebo_mission_designer_sitl_execution_request.v1"
)
SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION = "simulated_delivery_episode.v1"
PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT = "udp://127.0.0.1:14540"
PX4_GAZEBO_SITL_MISSION_UPLOAD_HOST = "127.0.0.1"
PX4_GAZEBO_SITL_MISSION_UPLOAD_PORT = 14540

_DEFAULT_PHASE_LABELS = (
    "preflight",
    "takeoff",
    "waypoint_route",
    "pickup_approach",
    "pickup_verified",
    "delivery_route",
    "dropoff_approach",
    "dropoff_verified",
    "return_land",
    "completed",
)
_DEFAULT_GATE_LABELS = (
    "health_snapshot_required",
    "phase_gate_evaluation_required",
    "operator_boundary_required",
    "replay_timeline_required",
)
_COMMAND_LIKE_LITERAL_PATTERNS = (
    "command_long",
    "mission_item",
    "mission_item_int",
    "mav_cmd",
    "mavlink",
    "udp:",
    "tcp:",
    "socket",
    "127.0.0.1",
    "localhost",
    "/cmd_vel",
    "/mavlink",
    "/fmu/",
    "docker run",
    "ros topic pub",
)
_COMMAND_LIKE_REGEX_PATTERNS = (
    ("port", re.compile(r"\bport\s*[:=]?\s*\d{2,5}\b", re.IGNORECASE)),
)
EARTH_RADIUS_M = 6371000.0
_FULLWIDTH_NUMBER_TRANSLATION = str.maketrans(
    "０１２３４５６７８９．，、",
    "0123456789.,,",
)
_ALTITUDE_PATTERNS = (
    re.compile(
        r"(?P<value>\d+(?:[.,]\d+)?)\s*(?:m|meter|meters|metre|metres|メートル|ｍ)",
        re.IGNORECASE,
    ),
)
_PAYLOAD_WEIGHT_PATTERNS = (
    re.compile(
        r"(?P<value>\d+(?:[.,]\d+)?)\s*(?:kg|kgs|kilogram|kilograms|キログラム|キロ(?!\s*(?:先|メートル)))",
        re.IGNORECASE,
    ),
)


class PX4GazeboMissionScenarioDesignerError(RuntimeError):
    """Raised when a prompt-derived mission scenario artifact is unsafe."""


class PX4GazeboMissionScenarioValidationStatus(str, Enum):
    ACCEPTED = "accepted"
    BLOCKED = "blocked"


class PX4GazeboMissionScenarioDryRunStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


class _ScenarioSafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    scenario_proposal_only: Literal[True] = True
    operator_review_required: Literal[True] = True
    llm_output_is_authority: Literal[False] = False
    llm_grants_dispatch_authority: Literal[False] = False
    scenario_grants_gazebo_execution_authority: Literal[False] = False
    gazebo_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False
    approval_free_dispatch_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    memory_direct_command_authority_allowed: Literal[False] = False


class _BoundedSimulationSafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    operator_review_required: Literal[True] = True
    llm_output_is_authority: Literal[False] = False
    llm_grants_dispatch_authority: Literal[False] = False
    scenario_grants_gazebo_execution_authority: Literal[False] = False
    gazebo_execution_invoked: Literal[False] = False
    deterministic_bounded_runner_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False
    approval_free_dispatch_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    memory_direct_command_authority_allowed: Literal[False] = False


class _PreparedSITLExecutionSafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    sitl_only: Literal[True] = True
    execution_prepared: Literal[True] = True
    execution_invoked: Literal[False] = False
    requires_explicit_execution_approval: Literal[True] = True
    operator_review_required: Literal[True] = True
    llm_output_is_authority: Literal[False] = False
    scenario_grants_gazebo_execution_authority: Literal[False] = False
    gazebo_execution_invoked: Literal[False] = False
    external_dispatch_performed: Literal[False] = False
    mavlink_dispatch_performed: Literal[False] = False
    px4_mission_upload_performed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    deterministic_bounded_runner_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_hardware_target: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False
    approval_free_dispatch_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    memory_direct_command_authority_allowed: Literal[False] = False


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _ordered_strings(values: Iterable[str] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values or ():
        text = str(value).strip().lower().replace(" ", "_")
        text = re.sub(r"[^a-z0-9_\-]+", "_", text).strip("_")
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _ordered_refs(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(
        sorted({str(item).strip() for item in (values or ()) if str(item).strip()})
    )


def _lesson_ref(lesson: DeliveryMissionLesson) -> str:
    return f"delivery_mission_lesson:{lesson.lesson_id}"


def _lesson_registry_snapshot_hash(
    *, used_refs: Sequence[str], ignored_refs: Sequence[str]
) -> str:
    payload = {
        "used_lesson_refs": _ordered_refs(used_refs),
        "ignored_lesson_refs": _ordered_refs(ignored_refs),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    return (
        "lesson_registry_snapshot_" + sha256(encoded.encode("utf-8")).hexdigest()[:16]
    )


def _prompt_text(prompt: str) -> str:
    text = str(prompt or "").strip()
    if not text:
        raise PX4GazeboMissionScenarioDesignerError(
            "mission scenario prompt is required"
        )
    if len(text) > 4000:
        raise PX4GazeboMissionScenarioDesignerError(
            "mission scenario prompt must be 4000 characters or less"
        )
    return text


def _contains_any(text: str, tokens: Sequence[str]) -> bool:
    lowered = text.lower()
    for token in tokens:
        normalized = token.lower()
        if (
            normalized
            and normalized.isascii()
            and normalized[0].isalnum()
            and normalized[-1].isalnum()
        ):
            if re.search(rf"\b{re.escape(normalized)}\b", lowered):
                return True
            continue
        if normalized in lowered:
            return True
    return False


def _normalized_numeric_text(text: str) -> str:
    return text.translate(_FULLWIDTH_NUMBER_TRANSLATION)


def _extract_number(patterns: Sequence[re.Pattern[str]], prompt: str) -> float | None:
    normalized = _normalized_numeric_text(prompt)
    for pattern in patterns:
        match = pattern.search(normalized)
        if not match:
            continue
        value = match.group("value").replace(",", "")
        try:
            return float(value)
        except ValueError:
            continue
    return None


def _mission_envelope_from_proposal_inputs(
    *,
    altitude_target_m: int | None,
    payload_weight_kg: float | None,
    hazards: Mapping[str, tuple[str, ...]],
) -> MissionEnvelope:
    terrain = None
    if "mountain_route" in hazards.get("terrain_hazard_labels", ()):
        terrain = "mountain"
    return MissionEnvelope(
        vehicle_class="px4_sitl",
        payload_kg=payload_weight_kg,
        altitude_m=float(altitude_target_m) if altitude_target_m is not None else None,
        terrain_class=terrain,
        mission_profile="delivery",
    )


def _lesson_ignore_reason(
    lesson: DeliveryMissionLesson,
    envelope: MissionEnvelope,
    *,
    episode_schema_version: str,
    now: datetime | None,
) -> str:
    if episode_schema_version not in lesson.valid_for_episode_schema_versions:
        return "episode_schema_version_mismatch"
    if lesson.superseded_by_lesson_ref or (
        lesson.expired_at is not None and lesson.expired_at <= _utc(now)
    ):
        return "superseded_or_expired"
    return "applicability_mismatch"


def _resolve_lesson_surfaces(
    *,
    lesson_registry: Sequence[DeliveryMissionLesson | Mapping[str, Any]] | None,
    envelope: MissionEnvelope,
    episode_schema_version: str,
    now: datetime | None,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[IgnoredLessonRecord, ...],
    tuple[SuppressedScenario, ...],
]:
    used: list[str] = []
    ignored: list[str] = []
    ignored_records: list[IgnoredLessonRecord] = []
    suppressed: list[SuppressedScenario] = []
    for raw in lesson_registry or ():
        lesson = (
            raw
            if isinstance(raw, DeliveryMissionLesson)
            else DeliveryMissionLesson.model_validate(dict(raw))
        )
        ref = _lesson_ref(lesson)
        if lesson_applies_to(
            lesson,
            envelope,
            episode_schema_version=episode_schema_version,
            now=now,
        ):
            used.append(ref)
            if lesson.recommendation.avoid_scenario_summary:
                suppressed.append(
                    SuppressedScenario(
                        scenario_summary=lesson.recommendation.avoid_scenario_summary,
                        suppressing_lesson_ref=ref,
                        suppression_rationale=lesson.recommendation.design_hint,
                    )
                )
            continue
        ignored.append(ref)
        ignored_records.append(
            IgnoredLessonRecord(
                lesson_ref=ref,
                ignore_reason=_lesson_ignore_reason(
                    lesson,
                    envelope,
                    episode_schema_version=episode_schema_version,
                    now=now,
                ),
            )
        )
    return (
        _ordered_refs(used),
        _ordered_refs(ignored),
        tuple(sorted(ignored_records, key=lambda item: item.lesson_ref)),
        tuple(sorted(suppressed, key=lambda item: item.suppressing_lesson_ref)),
    )


def _extract_altitude_target_m(prompt: str) -> int | None:
    value = _extract_number(_ALTITUDE_PATTERNS, prompt)
    if value is None:
        return None
    return int(round(value))


def _extract_payload_weight_kg(prompt: str) -> float | None:
    value = _extract_number(_PAYLOAD_WEIGHT_PATTERNS, prompt)
    if value is None:
        return None
    return float(value)


def _constraint_labels(
    *,
    altitude_target_m: int | None,
    payload_weight_kg: float | None,
) -> tuple[str, ...]:
    labels: list[str] = []
    if altitude_target_m is not None:
        labels.append("altitude_target_m")
    if payload_weight_kg is not None:
        labels.append("payload_weight_kg")
    return _ordered_strings(labels)


def _feasibility_risk_labels(
    *,
    altitude_target_m: int | None,
    payload_weight_kg: float | None,
) -> tuple[str, ...]:
    risks: list[str] = []
    if altitude_target_m is not None and altitude_target_m >= 2500:
        risks.append("high_altitude_density_altitude")
    if payload_weight_kg is not None and payload_weight_kg >= 5.0:
        risks.append("payload_margin_risk")
    if (
        altitude_target_m is not None
        and altitude_target_m >= 2500
        and payload_weight_kg is not None
        and payload_weight_kg >= 5.0
    ):
        risks.append("energy_budget_risk")
    return _ordered_strings(risks)


def _hazard_labels(
    prompt: str,
    *,
    altitude_target_m: int | None = None,
    payload_weight_kg: float | None = None,
) -> dict[str, tuple[str, ...]]:
    lowered = prompt.lower()
    weather: list[str] = []
    terrain: list[str] = []
    equipment: list[str] = []

    if _contains_any(
        lowered, ("wind", "windy", "strong wind", "gust", "gusty", "強風", "風")
    ):
        weather.append("strong_wind")
    if _contains_any(lowered, ("rain", "雨", "storm", "thunder", "雷")):
        weather.append("rain_or_storm")
    if _contains_any(lowered, ("fog", "霧", "visibility", "視界")):
        weather.append("low_visibility")

    if _contains_any(lowered, ("slope", "hill", "mountain", "山", "坂", "斜面")):
        terrain.append("slope_or_elevation")
    if _contains_any(lowered, ("mountain", "mountainous", "山", "山岳")):
        terrain.append("mountain_route")
    if _contains_any(lowered, ("summit", "peak", "山頂", "頂上")):
        terrain.append("summit_dropoff")
    if altitude_target_m is not None and altitude_target_m >= 2500:
        terrain.append("high_elevation")
    if _contains_any(lowered, ("high altitude", "high elevation", "高高度")):
        terrain.append("high_elevation")
    if _contains_any(
        lowered, ("building", "urban", "narrow", "obstacle", "建物", "障害物", "狭い")
    ):
        terrain.append("obstacle_or_urban_corridor")
    if _contains_any(lowered, ("rough", "uneven", "terrain", "地形", "不整地")):
        terrain.append("rough_terrain")

    if _contains_any(lowered, ("battery", "low battery", "バッテリー", "電池")):
        equipment.append("battery_margin")
    if payload_weight_kg is not None or _contains_any(
        lowered, ("payload", "weight", "kg", "kilogram", "重さ", "重量", "キロ")
    ):
        equipment.append("payload_weight")
    if _contains_any(
        lowered, ("motor", "propeller", "actuator", "モーター", "プロペラ")
    ):
        equipment.append("propulsion_check")
    if _contains_any(
        lowered, ("sensor", "gps", "camera", "センサー", "カメラ", "測位")
    ):
        equipment.append("sensor_confidence")
    if _contains_any(lowered, ("link", "通信", "heartbeat", "telemetry")):
        equipment.append("link_health")

    return {
        "weather_hazard_labels": _ordered_strings(weather),
        "terrain_hazard_labels": _ordered_strings(terrain),
        "equipment_incident_labels": _ordered_strings(equipment),
    }


def _blocked_reasons_from_text(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    reasons = [
        f"prompt_contains_low_level_detail:{pattern.replace(' ', '_').replace('/', '')}"
        for pattern in _COMMAND_LIKE_LITERAL_PATTERNS
        if pattern in lowered
    ]
    reasons.extend(
        f"prompt_contains_low_level_detail:{label}"
        for label, pattern in _COMMAND_LIKE_REGEX_PATTERNS
        if pattern.search(text)
    )
    return _ordered_strings(reasons)


def _objective(prompt: str) -> str:
    compact = " ".join(prompt.split())
    if len(compact) <= 160:
        return compact
    return f"{compact[:157].rstrip()}..."


def _request_ref(request: "PX4GazeboMissionPromptRequest") -> str:
    return f"px4_gazebo_mission_prompt_request:{request.request_id}"


def _proposal_ref(proposal: "PX4GazeboMissionScenarioProposal") -> str:
    return f"px4_gazebo_mission_scenario_proposal:{proposal.proposal_id}"


def _validation_ref(validation: "PX4GazeboMissionScenarioValidationResult") -> str:
    return f"px4_gazebo_mission_scenario_validation_result:{validation.validation_id}"


def _approval_ref(approval: "PX4GazeboMissionScenarioApproval") -> str:
    return f"px4_gazebo_mission_scenario_approval:{approval.approval_id}"


def _compile_ref(compile_result: "PX4GazeboMissionScenarioCompileResult") -> str:
    return (
        f"px4_gazebo_mission_scenario_compile_result:{compile_result.compile_result_id}"
    )


def _bounded_request_ref(request: "PX4GazeboBoundedSimulationRequest") -> str:
    return f"px4_gazebo_bounded_simulation_request:{request.request_id}"


class PX4GazeboMissionPromptRequest(_ScenarioSafetyBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_MISSION_PROMPT_REQUEST_SCHEMA_VERSION] = (
        PX4_GAZEBO_MISSION_PROMPT_REQUEST_SCHEMA_VERSION
    )
    request_id: str
    prompt: str = Field(min_length=1, max_length=4000)
    prompt_source: Literal["operator_prompt"] = "operator_prompt"
    requested_generation_mode: Literal["prompt_to_scenario_proposal"] = (
        "prompt_to_scenario_proposal"
    )
    generated_at: datetime

    @field_validator("request_id", "prompt")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("field must be non-empty")
        return text


class IgnoredLessonRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lesson_ref: str
    ignore_reason: Literal[
        "applicability_mismatch",
        "episode_schema_version_mismatch",
        "superseded_or_expired",
    ]

    @model_validator(mode="after")
    def _validate_record(self) -> "IgnoredLessonRecord":
        if not self.lesson_ref.startswith("delivery_mission_lesson:"):
            raise ValueError("ignored lesson record requires lesson ref")
        return self


class SuppressedScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_summary: str
    suppressing_lesson_ref: str
    suppression_rationale: str

    @model_validator(mode="after")
    def _validate_suppression(self) -> "SuppressedScenario":
        if not self.scenario_summary.strip():
            raise ValueError("suppressed scenario requires summary")
        if not self.suppressing_lesson_ref.startswith("delivery_mission_lesson:"):
            raise ValueError("suppressed scenario requires lesson ref")
        if not self.suppression_rationale.strip():
            raise ValueError("suppressed scenario requires rationale")
        return self


class PX4GazeboMissionScenarioProposal(_ScenarioSafetyBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_MISSION_SCENARIO_PROPOSAL_SCHEMA_VERSION] = (
        PX4_GAZEBO_MISSION_SCENARIO_PROPOSAL_SCHEMA_VERSION
    )
    proposal_id: str
    prompt_request_ref: str
    mission_objective: str
    proposal_generation_mode: Literal["deterministic_safe_parser"] = (
        "deterministic_safe_parser"
    )
    mission_phase_labels: tuple[str, ...]
    weather_hazard_labels: tuple[str, ...] = ()
    terrain_hazard_labels: tuple[str, ...] = ()
    equipment_incident_labels: tuple[str, ...] = ()
    altitude_target_m: int | None = Field(default=None, ge=0, le=10000)
    payload_weight_kg: float | None = Field(default=None, ge=0, le=100)
    extracted_constraint_labels: tuple[str, ...] = ()
    feasibility_risk_labels: tuple[str, ...] = ()
    expected_gate_labels: tuple[str, ...] = _DEFAULT_GATE_LABELS
    proposed_waypoint_count: int = Field(ge=3, le=8)
    proposed_route_segment_count: int = Field(ge=3, le=8)
    scenario_review_notes: tuple[str, ...]
    used_lesson_refs: tuple[str, ...] = ()
    ignored_lesson_refs: tuple[str, ...] = ()
    ignored_lesson_records: tuple[IgnoredLessonRecord, ...] = ()
    suppressed_scenario_candidates: tuple[SuppressedScenario, ...] = ()
    verifier_contract_ref: str
    lesson_registry_snapshot_hash: str
    proposal_uses_lesson_authority_for_judgement: Literal[False] = False
    proposal_modifies_verifier_predicates: Literal[False] = False
    gazebo_test_status: Literal["deferred_until_operator_approval"] = (
        "deferred_until_operator_approval"
    )

    @field_validator(
        "used_lesson_refs",
        "ignored_lesson_refs",
        "ignored_lesson_records",
        "suppressed_scenario_candidates",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if isinstance(value, tuple):
            return value
        if isinstance(value, list):
            return tuple(value)
        return tuple(value)

    @model_validator(mode="after")
    def _validate_proposal(self) -> "PX4GazeboMissionScenarioProposal":
        if not self.prompt_request_ref.startswith("px4_gazebo_mission_prompt_request:"):
            raise ValueError("proposal requires prompt request ref")
        if len(self.mission_phase_labels) < 3:
            raise ValueError("proposal requires mission phases")
        if self.proposed_route_segment_count > self.proposed_waypoint_count:
            raise ValueError("route segment count cannot exceed waypoint count")
        if not self.verifier_contract_ref.startswith("verifier_contract:"):
            raise ValueError("proposal requires verifier contract ref")
        used = set(self.used_lesson_refs)
        ignored = set(self.ignored_lesson_refs)
        if used & ignored:
            raise ValueError("lesson cannot be both used and ignored")
        record_refs = {record.lesson_ref for record in self.ignored_lesson_records}
        if record_refs != ignored:
            raise ValueError("ignored lesson records must match ignored refs")
        for ref in used | ignored:
            if not ref.startswith("delivery_mission_lesson:"):
                raise ValueError("lesson refs must use delivery_mission_lesson prefix")
        for item in self.suppressed_scenario_candidates:
            if item.suppressing_lesson_ref not in used:
                raise ValueError("suppressed scenario requires a used lesson ref")
        expected_hash = _lesson_registry_snapshot_hash(
            used_refs=self.used_lesson_refs,
            ignored_refs=self.ignored_lesson_refs,
        )
        if self.lesson_registry_snapshot_hash != expected_hash:
            raise ValueError("lesson registry snapshot hash mismatch")
        return self


class PX4GazeboMissionScenarioValidationResult(_ScenarioSafetyBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_SCENARIO_VALIDATION_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_SCENARIO_VALIDATION_RESULT_SCHEMA_VERSION
    validation_id: str
    prompt_request_ref: str
    proposal_ref: str
    validation_status: PX4GazeboMissionScenarioValidationStatus
    blocked_reasons: tuple[str, ...] = ()
    accepted_for_operator_review: bool
    accepted_for_gazebo_execution: Literal[False] = False
    scenario_requires_operator_approval: Literal[True] = True

    @model_validator(mode="after")
    def _validate_status(self) -> "PX4GazeboMissionScenarioValidationResult":
        if self.validation_status is PX4GazeboMissionScenarioValidationStatus.ACCEPTED:
            if self.blocked_reasons:
                raise ValueError(
                    "accepted scenario validation cannot have blocked reasons"
                )
            if not self.accepted_for_operator_review:
                raise ValueError(
                    "accepted scenario validation requires operator review acceptance"
                )
        else:
            if not self.blocked_reasons:
                raise ValueError("blocked scenario validation requires blocked reasons")
            if self.accepted_for_operator_review:
                raise ValueError(
                    "blocked scenario validation cannot be accepted for review"
                )
        return self


class PX4GazeboMissionScenarioDryRunResult(_ScenarioSafetyBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_SCENARIO_DRY_RUN_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_SCENARIO_DRY_RUN_RESULT_SCHEMA_VERSION
    dry_run_id: str
    prompt_request_ref: str
    proposal_ref: str
    validation_ref: str
    dry_run_status: PX4GazeboMissionScenarioDryRunStatus
    dry_run_mode: Literal["artifact_only_no_gazebo_execution"] = (
        "artifact_only_no_gazebo_execution"
    )
    route_segment_count: int = Field(ge=0, le=8)
    blocked_reasons: tuple[str, ...] = ()
    report_summary: str

    @model_validator(mode="after")
    def _validate_dry_run(self) -> "PX4GazeboMissionScenarioDryRunResult":
        if self.dry_run_status is PX4GazeboMissionScenarioDryRunStatus.COMPLETED:
            if self.blocked_reasons:
                raise ValueError("completed dry run cannot have blocked reasons")
        else:
            if not self.blocked_reasons:
                raise ValueError("blocked dry run requires blocked reasons")
        return self


class PX4GazeboMissionScenarioApproval(_BoundedSimulationSafetyBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_MISSION_SCENARIO_APPROVAL_SCHEMA_VERSION] = (
        PX4_GAZEBO_MISSION_SCENARIO_APPROVAL_SCHEMA_VERSION
    )
    approval_id: str
    scenario_proposal_ref: str
    validation_ref: str
    operator_approved: Literal[True] = True
    approval_scope: Literal["compile_to_bounded_simulation_request_only"] = (
        "compile_to_bounded_simulation_request_only"
    )
    approved_for_bounded_simulation_request: Literal[True] = True
    approved_for_gazebo_execution: Literal[False] = False
    approved_for_hardware: Literal[False] = False
    approved_for_physical_execution: Literal[False] = False
    approved_at: datetime

    @model_validator(mode="after")
    def _validate_refs(self) -> "PX4GazeboMissionScenarioApproval":
        if not self.scenario_proposal_ref.startswith(
            "px4_gazebo_mission_scenario_proposal:"
        ):
            raise ValueError("scenario approval requires proposal ref")
        if not self.validation_ref.startswith(
            "px4_gazebo_mission_scenario_validation_result:"
        ):
            raise ValueError("scenario approval requires validation ref")
        return self


class PX4GazeboMissionScenarioCompileResult(_BoundedSimulationSafetyBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_SCENARIO_COMPILE_RESULT_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_SCENARIO_COMPILE_RESULT_SCHEMA_VERSION
    compile_result_id: str
    scenario_proposal_ref: str
    validation_ref: str
    approval_ref: str
    compile_status: Literal["compiled"] = "compiled"
    scenario_profile: Literal[
        "generic_bounded_delivery",
        "mountain_summit_payload_delivery",
    ]
    route_profile: Literal["standard_bounded_route", "staged_ascent_required"]
    runner_kind: Literal["deterministic_bounded_mission_runner"] = (
        "deterministic_bounded_mission_runner"
    )
    allowed_runner: Literal["deterministic_bounded_mission_runner"] = (
        "deterministic_bounded_mission_runner"
    )
    altitude_target_m: int | None = Field(default=None, ge=0, le=10000)
    payload_weight_kg: float | None = Field(default=None, ge=0, le=100)
    risk_profile: tuple[str, ...] = ()
    approval_scope: Literal["compile_to_bounded_simulation_request_only"] = (
        "compile_to_bounded_simulation_request_only"
    )
    compile_reason: str
    approved_for_bounded_simulation: Literal[True] = True
    approved_for_gazebo_execution: Literal[False] = False
    gazebo_execution_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_compile_result(self) -> "PX4GazeboMissionScenarioCompileResult":
        if not self.scenario_proposal_ref.startswith(
            "px4_gazebo_mission_scenario_proposal:"
        ):
            raise ValueError("scenario compile result requires proposal ref")
        if not self.validation_ref.startswith(
            "px4_gazebo_mission_scenario_validation_result:"
        ):
            raise ValueError("scenario compile result requires validation ref")
        if not self.approval_ref.startswith("px4_gazebo_mission_scenario_approval:"):
            raise ValueError("scenario compile result requires approval ref")
        return self


class PX4GazeboBoundedSimulationRequest(_BoundedSimulationSafetyBoundary):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_BOUNDED_SIMULATION_REQUEST_SCHEMA_VERSION] = (
        PX4_GAZEBO_BOUNDED_SIMULATION_REQUEST_SCHEMA_VERSION
    )
    request_id: str
    scenario_proposal_ref: str
    validation_ref: str
    approval_ref: str
    compile_result_ref: str
    request_status: Literal["ready_for_deterministic_bounded_run"] = (
        "ready_for_deterministic_bounded_run"
    )
    operator_approved: Literal[True] = True
    approved_for_bounded_simulation: Literal[True] = True
    approved_for_gazebo_execution: Literal[False] = False
    approval_scope: Literal["compile_to_bounded_simulation_request_only"] = (
        "compile_to_bounded_simulation_request_only"
    )
    bounded_runner_selected: Literal[True] = True
    runner_kind: Literal["deterministic_bounded_mission_runner"] = (
        "deterministic_bounded_mission_runner"
    )
    scenario_profile: Literal[
        "generic_bounded_delivery",
        "mountain_summit_payload_delivery",
    ]
    route_profile: Literal["standard_bounded_route", "staged_ascent_required"]
    altitude_target_m: int | None = Field(default=None, ge=0, le=10000)
    payload_weight_kg: float | None = Field(default=None, ge=0, le=100)
    risk_profile: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_request_refs(self) -> "PX4GazeboBoundedSimulationRequest":
        if not self.scenario_proposal_ref.startswith(
            "px4_gazebo_mission_scenario_proposal:"
        ):
            raise ValueError("bounded simulation request requires proposal ref")
        if not self.validation_ref.startswith(
            "px4_gazebo_mission_scenario_validation_result:"
        ):
            raise ValueError("bounded simulation request requires validation ref")
        if not self.approval_ref.startswith("px4_gazebo_mission_scenario_approval:"):
            raise ValueError("bounded simulation request requires approval ref")
        if not self.compile_result_ref.startswith(
            "px4_gazebo_mission_scenario_compile_result:"
        ):
            raise ValueError("bounded simulation request requires compile result ref")
        return self


class PX4GazeboMissionDesignerSITLExecutionRequest(
    _PreparedSITLExecutionSafetyBoundary
):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_REQUEST_SCHEMA_VERSION
    ] = PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_REQUEST_SCHEMA_VERSION
    execution_request_id: str
    scenario_proposal_ref: str
    validation_ref: str
    approval_ref: str
    compile_result_ref: str
    bounded_simulation_request_ref: str
    request_status: Literal["prepared_waiting_for_sitl_execution_approval"] = (
        "prepared_waiting_for_sitl_execution_approval"
    )
    preparation_scope: Literal["prepare_sitl_execution_request_only"] = (
        "prepare_sitl_execution_request_only"
    )
    execution_mode: Literal["px4_gazebo_sitl_mission_upload"] = (
        "px4_gazebo_sitl_mission_upload"
    )
    target_endpoint: Literal[PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT] = (
        PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT
    )
    target_host: Literal[PX4_GAZEBO_SITL_MISSION_UPLOAD_HOST] = (
        PX4_GAZEBO_SITL_MISSION_UPLOAD_HOST
    )
    target_port: Literal[PX4_GAZEBO_SITL_MISSION_UPLOAD_PORT] = (
        PX4_GAZEBO_SITL_MISSION_UPLOAD_PORT
    )
    target_endpoint_whitelisted: Literal[True] = True
    scenario_profile: Literal[
        "generic_bounded_delivery",
        "mountain_summit_payload_delivery",
    ]
    route_profile: Literal["standard_bounded_route", "staged_ascent_required"]
    risk_profile: tuple[str, ...] = ()
    prepared_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("risk_profile", mode="before")
    @classmethod
    def _coerce_risk_profile(cls, value: Any) -> tuple[str, ...]:
        return _ordered_strings(value or ())

    @field_validator("prepared_at", mode="before")
    @classmethod
    def _coerce_prepared_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_execution_request(
        self,
    ) -> "PX4GazeboMissionDesignerSITLExecutionRequest":
        if not self.scenario_proposal_ref.startswith(
            "px4_gazebo_mission_scenario_proposal:"
        ):
            raise PX4GazeboMissionScenarioDesignerError(
                "SITL execution request requires scenario proposal ref"
            )
        if not self.validation_ref.startswith(
            "px4_gazebo_mission_scenario_validation_result:"
        ):
            raise PX4GazeboMissionScenarioDesignerError(
                "SITL execution request requires validation ref"
            )
        if not self.approval_ref.startswith("px4_gazebo_mission_scenario_approval:"):
            raise PX4GazeboMissionScenarioDesignerError(
                "SITL execution request requires approval ref"
            )
        if not self.compile_result_ref.startswith(
            "px4_gazebo_mission_scenario_compile_result:"
        ):
            raise PX4GazeboMissionScenarioDesignerError(
                "SITL execution request requires compile result ref"
            )
        if not self.bounded_simulation_request_ref.startswith(
            "px4_gazebo_bounded_simulation_request:"
        ):
            raise PX4GazeboMissionScenarioDesignerError(
                "SITL execution request requires bounded simulation request ref"
            )
        raise_for_command_like_payload(
            self.metadata,
            root="mission_designer_sitl_execution_request.metadata",
            error_type=PX4GazeboMissionScenarioDesignerError,
            prefix="SITL execution request refused command-like metadata",
        )
        return self


def build_px4_gazebo_mission_prompt_request(
    *,
    prompt: str,
    now: datetime | None = None,
) -> PX4GazeboMissionPromptRequest:
    text = _prompt_text(prompt)
    generated_at = _utc(now)
    payload = {"prompt": text, "generated_at": generated_at.isoformat()}
    return PX4GazeboMissionPromptRequest(
        request_id=_stable_id("px4_gazebo_mission_prompt_request", payload),
        prompt=text,
        generated_at=generated_at,
    )


def build_px4_gazebo_mission_scenario_proposal(
    *,
    prompt_request: PX4GazeboMissionPromptRequest | Mapping[str, Any],
    lesson_registry: Sequence[DeliveryMissionLesson | Mapping[str, Any]] | None = None,
    verifier_contract_ref: str | None = None,
    episode_schema_version: str = SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    now: datetime | None = None,
) -> PX4GazeboMissionScenarioProposal:
    request = (
        prompt_request
        if isinstance(prompt_request, PX4GazeboMissionPromptRequest)
        else PX4GazeboMissionPromptRequest.model_validate(prompt_request)
    )
    altitude_target_m = _extract_altitude_target_m(request.prompt)
    payload_weight_kg = _extract_payload_weight_kg(request.prompt)
    hazards = _hazard_labels(
        request.prompt,
        altitude_target_m=altitude_target_m,
        payload_weight_kg=payload_weight_kg,
    )
    constraint_labels = _constraint_labels(
        altitude_target_m=altitude_target_m,
        payload_weight_kg=payload_weight_kg,
    )
    feasibility_risks = _feasibility_risk_labels(
        altitude_target_m=altitude_target_m,
        payload_weight_kg=payload_weight_kg,
    )
    waypoint_count = 3
    if _contains_any(request.prompt, ("multi", "複数", "3 waypoint", "three waypoint")):
        waypoint_count = 4
    if _contains_any(request.prompt, ("complex", "複雑", "many", "多数")):
        waypoint_count = max(waypoint_count, 5)
    route_segment_count = max(3, min(waypoint_count, 4))
    review_notes = (
        "Prompt-derived scenario is a proposal for operator review only.",
        "Gazebo execution remains deferred until explicit approval and bounded runner selection.",
        "LLM or prompt output cannot grant dispatch authority.",
    )
    envelope = _mission_envelope_from_proposal_inputs(
        altitude_target_m=altitude_target_m,
        payload_weight_kg=payload_weight_kg,
        hazards=hazards,
    )
    used_lesson_refs, ignored_lesson_refs, ignored_records, suppressed = (
        _resolve_lesson_surfaces(
            lesson_registry=lesson_registry,
            envelope=envelope,
            episode_schema_version=episode_schema_version,
            now=now,
        )
    )
    resolved_verifier_contract_ref = verifier_contract_ref or (
        f"verifier_contract:{current_verifier_contract(created_at=now).contract_id}"
    )
    lesson_registry_snapshot_hash = _lesson_registry_snapshot_hash(
        used_refs=used_lesson_refs,
        ignored_refs=ignored_lesson_refs,
    )
    payload = {
        "prompt_request_ref": _request_ref(request),
        "objective": _objective(request.prompt),
        "hazards": hazards,
        "constraints": constraint_labels,
        "altitude_target_m": altitude_target_m,
        "payload_weight_kg": payload_weight_kg,
        "feasibility_risks": feasibility_risks,
        "waypoints": waypoint_count,
        "used_lesson_refs": used_lesson_refs,
        "ignored_lesson_refs": ignored_lesson_refs,
        "ignored_lesson_records": [
            item.model_dump(mode="json") for item in ignored_records
        ],
        "suppressed_scenario_candidates": [
            item.model_dump(mode="json") for item in suppressed
        ],
        "verifier_contract_ref": resolved_verifier_contract_ref,
        "lesson_registry_snapshot_hash": lesson_registry_snapshot_hash,
    }
    return PX4GazeboMissionScenarioProposal(
        proposal_id=_stable_id("px4_gazebo_mission_scenario_proposal", payload),
        prompt_request_ref=_request_ref(request),
        mission_objective=_objective(request.prompt),
        mission_phase_labels=_DEFAULT_PHASE_LABELS,
        weather_hazard_labels=hazards["weather_hazard_labels"],
        terrain_hazard_labels=hazards["terrain_hazard_labels"],
        equipment_incident_labels=hazards["equipment_incident_labels"],
        altitude_target_m=altitude_target_m,
        payload_weight_kg=payload_weight_kg,
        extracted_constraint_labels=constraint_labels,
        feasibility_risk_labels=feasibility_risks,
        proposed_waypoint_count=waypoint_count,
        proposed_route_segment_count=route_segment_count,
        scenario_review_notes=review_notes,
        used_lesson_refs=used_lesson_refs,
        ignored_lesson_refs=ignored_lesson_refs,
        ignored_lesson_records=ignored_records,
        suppressed_scenario_candidates=suppressed,
        verifier_contract_ref=resolved_verifier_contract_ref,
        lesson_registry_snapshot_hash=lesson_registry_snapshot_hash,
    )


def build_px4_gazebo_mission_scenario_validation_result(
    *,
    prompt_request: PX4GazeboMissionPromptRequest | Mapping[str, Any],
    proposal: PX4GazeboMissionScenarioProposal | Mapping[str, Any],
) -> PX4GazeboMissionScenarioValidationResult:
    request = (
        prompt_request
        if isinstance(prompt_request, PX4GazeboMissionPromptRequest)
        else PX4GazeboMissionPromptRequest.model_validate(prompt_request)
    )
    proposal_obj = (
        proposal
        if isinstance(proposal, PX4GazeboMissionScenarioProposal)
        else PX4GazeboMissionScenarioProposal.model_validate(proposal)
    )
    if proposal_obj.prompt_request_ref != _request_ref(request):
        raise PX4GazeboMissionScenarioDesignerError(
            "scenario proposal prompt request mismatch"
        )
    blocked_reasons = _blocked_reasons_from_text(request.prompt)
    status = (
        PX4GazeboMissionScenarioValidationStatus.BLOCKED
        if blocked_reasons
        else PX4GazeboMissionScenarioValidationStatus.ACCEPTED
    )
    payload = {
        "prompt_request_ref": _request_ref(request),
        "proposal_ref": _proposal_ref(proposal_obj),
        "blocked_reasons": blocked_reasons,
    }
    return PX4GazeboMissionScenarioValidationResult(
        validation_id=_stable_id("px4_gazebo_mission_scenario_validation", payload),
        prompt_request_ref=_request_ref(request),
        proposal_ref=_proposal_ref(proposal_obj),
        validation_status=status,
        blocked_reasons=blocked_reasons,
        accepted_for_operator_review=not blocked_reasons,
    )


def build_px4_gazebo_mission_scenario_dry_run_result(
    *,
    prompt_request: PX4GazeboMissionPromptRequest | Mapping[str, Any],
    proposal: PX4GazeboMissionScenarioProposal | Mapping[str, Any],
    validation: PX4GazeboMissionScenarioValidationResult | Mapping[str, Any],
) -> PX4GazeboMissionScenarioDryRunResult:
    request = (
        prompt_request
        if isinstance(prompt_request, PX4GazeboMissionPromptRequest)
        else PX4GazeboMissionPromptRequest.model_validate(prompt_request)
    )
    proposal_obj = (
        proposal
        if isinstance(proposal, PX4GazeboMissionScenarioProposal)
        else PX4GazeboMissionScenarioProposal.model_validate(proposal)
    )
    validation_obj = (
        validation
        if isinstance(validation, PX4GazeboMissionScenarioValidationResult)
        else PX4GazeboMissionScenarioValidationResult.model_validate(validation)
    )
    if validation_obj.prompt_request_ref != _request_ref(request):
        raise PX4GazeboMissionScenarioDesignerError(
            "scenario validation prompt request mismatch"
        )
    if validation_obj.proposal_ref != _proposal_ref(proposal_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "scenario validation proposal mismatch"
        )
    blocked = (
        validation_obj.validation_status
        is PX4GazeboMissionScenarioValidationStatus.BLOCKED
    )
    payload = {
        "prompt_request_ref": _request_ref(request),
        "proposal_ref": _proposal_ref(proposal_obj),
        "validation_ref": _validation_ref(validation_obj),
        "status": "blocked" if blocked else "completed",
    }
    status = (
        PX4GazeboMissionScenarioDryRunStatus.BLOCKED
        if blocked
        else PX4GazeboMissionScenarioDryRunStatus.COMPLETED
    )
    return PX4GazeboMissionScenarioDryRunResult(
        dry_run_id=_stable_id("px4_gazebo_mission_scenario_dry_run", payload),
        prompt_request_ref=_request_ref(request),
        proposal_ref=_proposal_ref(proposal_obj),
        validation_ref=_validation_ref(validation_obj),
        dry_run_status=status,
        route_segment_count=0 if blocked else proposal_obj.proposed_route_segment_count,
        blocked_reasons=validation_obj.blocked_reasons,
        report_summary=(
            "Scenario proposal is ready for operator review; Gazebo execution was not invoked."
            if not blocked
            else "Scenario proposal is blocked before operator review because it contains low-level execution detail."
        ),
    )


def _coerce_proposal(
    proposal: PX4GazeboMissionScenarioProposal | Mapping[str, Any],
) -> PX4GazeboMissionScenarioProposal:
    return (
        proposal
        if isinstance(proposal, PX4GazeboMissionScenarioProposal)
        else PX4GazeboMissionScenarioProposal.model_validate(proposal)
    )


def _coerce_validation(
    validation: PX4GazeboMissionScenarioValidationResult | Mapping[str, Any],
) -> PX4GazeboMissionScenarioValidationResult:
    return (
        validation
        if isinstance(validation, PX4GazeboMissionScenarioValidationResult)
        else PX4GazeboMissionScenarioValidationResult.model_validate(validation)
    )


def _coerce_approval(
    approval: PX4GazeboMissionScenarioApproval | Mapping[str, Any],
) -> PX4GazeboMissionScenarioApproval:
    return (
        approval
        if isinstance(approval, PX4GazeboMissionScenarioApproval)
        else PX4GazeboMissionScenarioApproval.model_validate(approval)
    )


def _coerce_compile_result(
    compile_result: PX4GazeboMissionScenarioCompileResult | Mapping[str, Any],
) -> PX4GazeboMissionScenarioCompileResult:
    return (
        compile_result
        if isinstance(compile_result, PX4GazeboMissionScenarioCompileResult)
        else PX4GazeboMissionScenarioCompileResult.model_validate(compile_result)
    )


def _coerce_bounded_request(
    request: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
) -> PX4GazeboBoundedSimulationRequest:
    return (
        request
        if isinstance(request, PX4GazeboBoundedSimulationRequest)
        else PX4GazeboBoundedSimulationRequest.model_validate(request)
    )


def _assert_validation_matches_proposal(
    *,
    proposal: PX4GazeboMissionScenarioProposal,
    validation: PX4GazeboMissionScenarioValidationResult,
) -> None:
    if validation.proposal_ref != _proposal_ref(proposal):
        raise PX4GazeboMissionScenarioDesignerError(
            "scenario validation proposal mismatch"
        )


def _scenario_profile(
    proposal: PX4GazeboMissionScenarioProposal,
) -> tuple[
    Literal["generic_bounded_delivery", "mountain_summit_payload_delivery"],
    Literal["standard_bounded_route", "staged_ascent_required"],
]:
    terrain = set(proposal.terrain_hazard_labels)
    risks = set(proposal.feasibility_risk_labels)
    if {
        "mountain_route",
        "summit_dropoff",
        "high_elevation",
    }.issubset(terrain) and {
        "high_altitude_density_altitude",
        "payload_margin_risk",
        "energy_budget_risk",
    }.issubset(risks):
        return "mountain_summit_payload_delivery", "staged_ascent_required"
    return "generic_bounded_delivery", "standard_bounded_route"


def _compile_reason(
    *,
    scenario_profile: str,
    route_profile: str,
    risk_profile: Sequence[str],
) -> str:
    if (
        scenario_profile == "mountain_summit_payload_delivery"
        and route_profile == "staged_ascent_required"
    ):
        risks = ", ".join(risk_profile) or "mission feasibility risk"
        return (
            "High-elevation mountain delivery with payload risk is compiled to "
            f"a staged ascent bounded simulation request because of {risks}."
        )
    return (
        "Accepted scenario is compiled to a generic deterministic bounded "
        "simulation request for operator-reviewed planning only."
    )


def build_px4_gazebo_mission_scenario_approval(
    *,
    proposal: PX4GazeboMissionScenarioProposal | Mapping[str, Any],
    validation: PX4GazeboMissionScenarioValidationResult | Mapping[str, Any],
    now: datetime | None = None,
) -> PX4GazeboMissionScenarioApproval:
    proposal_obj = _coerce_proposal(proposal)
    validation_obj = _coerce_validation(validation)
    _assert_validation_matches_proposal(
        proposal=proposal_obj, validation=validation_obj
    )
    if (
        validation_obj.validation_status
        is not PX4GazeboMissionScenarioValidationStatus.ACCEPTED
    ):
        raise PX4GazeboMissionScenarioDesignerError(
            "blocked scenario cannot be approved"
        )
    if not validation_obj.accepted_for_operator_review:
        raise PX4GazeboMissionScenarioDesignerError(
            "scenario approval requires accepted operator review validation"
        )
    approved_at = _utc(now)
    payload = {
        "scenario_proposal_ref": _proposal_ref(proposal_obj),
        "validation_ref": _validation_ref(validation_obj),
        "approved_at": approved_at.isoformat(),
    }
    return PX4GazeboMissionScenarioApproval(
        approval_id=_stable_id("px4_gazebo_mission_scenario_approval", payload),
        scenario_proposal_ref=_proposal_ref(proposal_obj),
        validation_ref=_validation_ref(validation_obj),
        approved_at=approved_at,
    )


def build_px4_gazebo_mission_scenario_compile_result(
    *,
    proposal: PX4GazeboMissionScenarioProposal | Mapping[str, Any],
    validation: PX4GazeboMissionScenarioValidationResult | Mapping[str, Any],
    approval: PX4GazeboMissionScenarioApproval | Mapping[str, Any],
) -> PX4GazeboMissionScenarioCompileResult:
    proposal_obj = _coerce_proposal(proposal)
    validation_obj = _coerce_validation(validation)
    approval_obj = _coerce_approval(approval)
    _assert_validation_matches_proposal(
        proposal=proposal_obj, validation=validation_obj
    )
    if approval_obj.scenario_proposal_ref != _proposal_ref(proposal_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "scenario approval proposal mismatch"
        )
    if approval_obj.validation_ref != _validation_ref(validation_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "scenario approval validation mismatch"
        )
    if _blocked_reasons_from_text(proposal_obj.mission_objective):
        raise PX4GazeboMissionScenarioDesignerError(
            "scenario compile rejects low-level detail"
        )
    scenario_profile, route_profile = _scenario_profile(proposal_obj)
    risk_profile = _ordered_strings(proposal_obj.feasibility_risk_labels)
    compile_reason = _compile_reason(
        scenario_profile=scenario_profile,
        route_profile=route_profile,
        risk_profile=risk_profile,
    )
    payload = {
        "scenario_proposal_ref": _proposal_ref(proposal_obj),
        "validation_ref": _validation_ref(validation_obj),
        "approval_ref": _approval_ref(approval_obj),
        "scenario_profile": scenario_profile,
        "route_profile": route_profile,
        "risk_profile": risk_profile,
        "compile_reason": compile_reason,
    }
    return PX4GazeboMissionScenarioCompileResult(
        compile_result_id=_stable_id(
            "px4_gazebo_mission_scenario_compile_result",
            payload,
        ),
        scenario_proposal_ref=_proposal_ref(proposal_obj),
        validation_ref=_validation_ref(validation_obj),
        approval_ref=_approval_ref(approval_obj),
        scenario_profile=scenario_profile,
        route_profile=route_profile,
        altitude_target_m=proposal_obj.altitude_target_m,
        payload_weight_kg=proposal_obj.payload_weight_kg,
        risk_profile=risk_profile,
        compile_reason=compile_reason,
    )


def build_px4_gazebo_bounded_simulation_request(
    *,
    proposal: PX4GazeboMissionScenarioProposal | Mapping[str, Any],
    validation: PX4GazeboMissionScenarioValidationResult | Mapping[str, Any],
    approval: PX4GazeboMissionScenarioApproval | Mapping[str, Any],
    compile_result: PX4GazeboMissionScenarioCompileResult | Mapping[str, Any],
) -> PX4GazeboBoundedSimulationRequest:
    proposal_obj = _coerce_proposal(proposal)
    validation_obj = _coerce_validation(validation)
    approval_obj = _coerce_approval(approval)
    compile_obj = _coerce_compile_result(compile_result)
    _assert_validation_matches_proposal(
        proposal=proposal_obj, validation=validation_obj
    )
    if approval_obj.scenario_proposal_ref != _proposal_ref(proposal_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "bounded request approval proposal mismatch"
        )
    if approval_obj.validation_ref != _validation_ref(validation_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "bounded request approval validation mismatch"
        )
    if compile_obj.scenario_proposal_ref != _proposal_ref(proposal_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "bounded request compile proposal mismatch"
        )
    if compile_obj.validation_ref != _validation_ref(validation_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "bounded request compile validation mismatch"
        )
    if compile_obj.approval_ref != _approval_ref(approval_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "bounded request compile approval mismatch"
        )
    payload = {
        "scenario_proposal_ref": _proposal_ref(proposal_obj),
        "validation_ref": _validation_ref(validation_obj),
        "approval_ref": _approval_ref(approval_obj),
        "compile_result_ref": _compile_ref(compile_obj),
        "scenario_profile": compile_obj.scenario_profile,
        "route_profile": compile_obj.route_profile,
        "risk_profile": compile_obj.risk_profile,
    }
    return PX4GazeboBoundedSimulationRequest(
        request_id=_stable_id("px4_gazebo_bounded_simulation_request", payload),
        scenario_proposal_ref=_proposal_ref(proposal_obj),
        validation_ref=_validation_ref(validation_obj),
        approval_ref=_approval_ref(approval_obj),
        compile_result_ref=_compile_ref(compile_obj),
        scenario_profile=compile_obj.scenario_profile,
        route_profile=compile_obj.route_profile,
        altitude_target_m=compile_obj.altitude_target_m,
        payload_weight_kg=compile_obj.payload_weight_kg,
        risk_profile=compile_obj.risk_profile,
    )


def build_px4_gazebo_mission_designer_sitl_execution_request(
    *,
    proposal: PX4GazeboMissionScenarioProposal | Mapping[str, Any],
    validation: PX4GazeboMissionScenarioValidationResult | Mapping[str, Any],
    approval: PX4GazeboMissionScenarioApproval | Mapping[str, Any],
    compile_result: PX4GazeboMissionScenarioCompileResult | Mapping[str, Any],
    bounded_simulation_request: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
    now: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PX4GazeboMissionDesignerSITLExecutionRequest:
    proposal_obj = _coerce_proposal(proposal)
    validation_obj = _coerce_validation(validation)
    approval_obj = _coerce_approval(approval)
    compile_obj = _coerce_compile_result(compile_result)
    bounded_request_obj = _coerce_bounded_request(bounded_simulation_request)
    _assert_validation_matches_proposal(
        proposal=proposal_obj,
        validation=validation_obj,
    )
    if approval_obj.scenario_proposal_ref != _proposal_ref(proposal_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request approval proposal mismatch"
        )
    if approval_obj.validation_ref != _validation_ref(validation_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request approval validation mismatch"
        )
    if compile_obj.scenario_proposal_ref != _proposal_ref(proposal_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request compile proposal mismatch"
        )
    if compile_obj.validation_ref != _validation_ref(validation_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request compile validation mismatch"
        )
    if compile_obj.approval_ref != _approval_ref(approval_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request compile approval mismatch"
        )
    if bounded_request_obj.scenario_proposal_ref != _proposal_ref(proposal_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request bounded request proposal mismatch"
        )
    if bounded_request_obj.validation_ref != _validation_ref(validation_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request bounded request validation mismatch"
        )
    if bounded_request_obj.approval_ref != _approval_ref(approval_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request bounded request approval mismatch"
        )
    if bounded_request_obj.compile_result_ref != _compile_ref(compile_obj):
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request bounded request compile mismatch"
        )
    # These checks duplicate Literal[False] validation on purpose. This builder is
    # the handoff into later SITL execution approval, so keep the boundary visible
    # even if callers pass already-validated upstream artifacts.
    if approval_obj.approved_for_gazebo_execution is not False:
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request requires non-execution scenario approval"
        )
    if bounded_request_obj.gazebo_execution_invoked is not False:
        raise PX4GazeboMissionScenarioDesignerError(
            "SITL execution request cannot derive from invoked bounded request"
        )
    prepared_at = _utc(now)
    payload = {
        "scenario_proposal_ref": _proposal_ref(proposal_obj),
        "validation_ref": _validation_ref(validation_obj),
        "approval_ref": _approval_ref(approval_obj),
        "compile_result_ref": _compile_ref(compile_obj),
        "bounded_simulation_request_ref": _bounded_request_ref(bounded_request_obj),
        "scenario_profile": bounded_request_obj.scenario_profile,
        "route_profile": bounded_request_obj.route_profile,
        "risk_profile": _ordered_strings(bounded_request_obj.risk_profile),
        "prepared_at": prepared_at.isoformat(),
    }
    return PX4GazeboMissionDesignerSITLExecutionRequest(
        execution_request_id=_stable_id(
            "px4_gazebo_mission_designer_sitl_execution_request",
            payload,
        ),
        scenario_proposal_ref=_proposal_ref(proposal_obj),
        validation_ref=_validation_ref(validation_obj),
        approval_ref=_approval_ref(approval_obj),
        compile_result_ref=_compile_ref(compile_obj),
        bounded_simulation_request_ref=_bounded_request_ref(bounded_request_obj),
        scenario_profile=bounded_request_obj.scenario_profile,
        route_profile=bounded_request_obj.route_profile,
        risk_profile=bounded_request_obj.risk_profile,
        prepared_at=prepared_at,
        metadata=dict(metadata or {}),
    )


def approve_px4_gazebo_mission_scenario_for_bounded_simulation(
    *,
    proposal: PX4GazeboMissionScenarioProposal | Mapping[str, Any],
    validation: PX4GazeboMissionScenarioValidationResult | Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    proposal_obj = _coerce_proposal(proposal)
    validation_obj = _coerce_validation(validation)
    approval = build_px4_gazebo_mission_scenario_approval(
        proposal=proposal_obj,
        validation=validation_obj,
        now=now,
    )
    compile_result = build_px4_gazebo_mission_scenario_compile_result(
        proposal=proposal_obj,
        validation=validation_obj,
        approval=approval,
    )
    request = build_px4_gazebo_bounded_simulation_request(
        proposal=proposal_obj,
        validation=validation_obj,
        approval=approval,
        compile_result=compile_result,
    )
    return {
        "scenario_approval": approval.model_dump(mode="json"),
        "scenario_compile_result": compile_result.model_dump(mode="json"),
        "bounded_simulation_request": request.model_dump(mode="json"),
        "summary": {
            "approval_status": "approved",
            "operator_approved": True,
            "scenario_profile": compile_result.scenario_profile,
            "route_profile": compile_result.route_profile,
            "runner_kind": request.runner_kind,
            "risk_profile": list(compile_result.risk_profile),
            "approval_scope": approval.approval_scope,
            "compile_reason": compile_result.compile_reason,
            "approved_for_bounded_simulation": True,
            "approved_for_gazebo_execution": False,
            "deterministic_bounded_runner_invoked": False,
            "gazebo_execution_invoked": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "px4_mission_upload_allowed": False,
            "unbounded_setpoint_stream_allowed": False,
        },
    }


def _coordinate_float(value: Any, *, field_name: str, low: float, high: float) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError) as exc:
        raise PX4GazeboMissionScenarioDesignerError(
            f"{field_name} must be numeric"
        ) from exc
    if not low <= resolved <= high:
        raise PX4GazeboMissionScenarioDesignerError(
            f"{field_name} must be between {low} and {high}"
        )
    return resolved


def _coordinate_positive_int(value: Any, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise PX4GazeboMissionScenarioDesignerError(
            f"{field_name} must be a positive integer"
        )
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise PX4GazeboMissionScenarioDesignerError(
            f"{field_name} must be a positive integer"
        ) from exc
    resolved = int(numeric)
    if numeric != float(resolved) or resolved <= 0:
        raise PX4GazeboMissionScenarioDesignerError(
            f"{field_name} must be a positive integer"
        )
    return resolved


def _haversine_distance_m(
    *,
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = math.radians(latitude_b - latitude_a)
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2.0) ** 2
    )
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(value), math.sqrt(1.0 - value))


def _bbox_for_coordinate_pair(
    *,
    takeoff_latitude: float,
    takeoff_longitude: float,
    dropoff_latitude: float,
    dropoff_longitude: float,
    margin_m: float = 100.0,
) -> tuple[float, float, float, float]:
    center_lat = (takeoff_latitude + dropoff_latitude) / 2.0
    lat_margin = margin_m / 111320.0
    lon_margin = margin_m / max(111320.0 * math.cos(math.radians(center_lat)), 1.0)
    return (
        round(min(takeoff_latitude, dropoff_latitude) - lat_margin, 7),
        round(min(takeoff_longitude, dropoff_longitude) - lon_margin, 7),
        round(max(takeoff_latitude, dropoff_latitude) + lat_margin, 7),
        round(max(takeoff_longitude, dropoff_longitude) + lon_margin, 7),
    )


def _coordinate_route_from_payload(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    takeoff_latitude = _coordinate_float(
        value.get("takeoff_latitude"),
        field_name="takeoff_latitude",
        low=-90.0,
        high=90.0,
    )
    takeoff_longitude = _coordinate_float(
        value.get("takeoff_longitude"),
        field_name="takeoff_longitude",
        low=-180.0,
        high=180.0,
    )
    dropoff_latitude = _coordinate_float(
        value.get("dropoff_latitude"),
        field_name="dropoff_latitude",
        low=-90.0,
        high=90.0,
    )
    dropoff_longitude = _coordinate_float(
        value.get("dropoff_longitude"),
        field_name="dropoff_longitude",
        low=-180.0,
        high=180.0,
    )
    roof_height_agl_m = _coordinate_float(
        value.get("dropoff_roof_height_agl_m", 0.0),
        field_name="dropoff_roof_height_agl_m",
        low=0.0,
        high=500.0,
    )
    payload_weight_kg = (
        _coordinate_float(
            value.get("payload_weight_kg"),
            field_name="payload_weight_kg",
            low=0.0,
            high=100.0,
        )
        if value.get("payload_weight_kg") not in (None, "")
        else None
    )
    wind_speed_mps = (
        _coordinate_float(
            value.get("wind_speed_mps"),
            field_name="wind_speed_mps",
            low=0.0,
            high=100.0,
        )
        if value.get("wind_speed_mps") not in (None, "")
        else None
    )
    wind_direction_deg = (
        _coordinate_float(
            value.get("wind_direction_deg"),
            field_name="wind_direction_deg",
            low=0.0,
            high=360.0,
        )
        if value.get("wind_direction_deg") not in (None, "")
        else None
    )
    wind_gust_mps = (
        _coordinate_float(
            value.get("wind_gust_mps"),
            field_name="wind_gust_mps",
            low=0.0,
            high=100.0,
        )
        if value.get("wind_gust_mps") not in (None, "")
        else None
    )
    wind_variance = (
        _coordinate_float(
            value.get("wind_variance"),
            field_name="wind_variance",
            low=0.0,
            high=100.0,
        )
        if value.get("wind_variance") not in (None, "")
        else None
    )
    temperature_c = (
        _coordinate_float(
            value.get("temperature_c"),
            field_name="temperature_c",
            low=-80.0,
            high=80.0,
        )
        if value.get("temperature_c") not in (None, "")
        else None
    )
    pressure_hpa = (
        _coordinate_float(
            value.get("pressure_hpa"),
            field_name="pressure_hpa",
            low=500.0,
            high=1100.0,
        )
        if value.get("pressure_hpa") not in (None, "")
        else None
    )
    precipitation_mm_per_hour = (
        _coordinate_float(
            value.get("precipitation_mm_per_hour"),
            field_name="precipitation_mm_per_hour",
            low=0.0,
            high=500.0,
        )
        if value.get("precipitation_mm_per_hour") not in (None, "")
        else None
    )
    rain_battery_drain_factor = (
        _coordinate_float(
            value.get("rain_battery_drain_factor"),
            field_name="rain_battery_drain_factor",
            low=0.1,
            high=10.0,
        )
        if value.get("rain_battery_drain_factor") not in (None, "")
        else None
    )
    rain_sensor_degradation_factor = (
        _coordinate_float(
            value.get("rain_sensor_degradation_factor"),
            field_name="rain_sensor_degradation_factor",
            low=0.0,
            high=1.0,
        )
        if value.get("rain_sensor_degradation_factor") not in (None, "")
        else None
    )
    rain_landing_risk_factor = (
        _coordinate_float(
            value.get("rain_landing_risk_factor"),
            field_name="rain_landing_risk_factor",
            low=1.0,
            high=10.0,
        )
        if value.get("rain_landing_risk_factor") not in (None, "")
        else None
    )
    thermal_battery_drain_factor = (
        _coordinate_float(
            value.get("thermal_battery_drain_factor"),
            field_name="thermal_battery_drain_factor",
            low=0.1,
            high=10.0,
        )
        if value.get("thermal_battery_drain_factor") not in (None, "")
        else None
    )
    thermal_motor_derate_factor = (
        _coordinate_float(
            value.get("thermal_motor_derate_factor"),
            field_name="thermal_motor_derate_factor",
            low=0.1,
            high=1.0,
        )
        if value.get("thermal_motor_derate_factor") not in (None, "")
        else None
    )
    terrain_clearance_agl_m = (
        _coordinate_float(
            value.get("terrain_clearance_agl_m")
            or value.get("terrain_clearance_target_m")
            or value.get("minimum_terrain_clearance_m"),
            field_name="terrain_clearance_agl_m",
            low=0.0,
            high=500.0,
        )
        if (
            value.get("terrain_clearance_agl_m")
            or value.get("terrain_clearance_target_m")
            or value.get("minimum_terrain_clearance_m")
        )
        not in (None, "")
        else None
    )
    terrain_profile_raw = value.get("terrain_profile") or value.get(
        "terrain_elevation_profile"
    )
    terrain_profile = (
        [
            dict(sample)
            for sample in terrain_profile_raw
            if isinstance(sample, Mapping)
        ]
        if isinstance(terrain_profile_raw, Sequence)
        and not isinstance(terrain_profile_raw, str | bytes)
        else []
    )
    auto_route_waypoint_count = _coordinate_positive_int(
        value.get("auto_route_waypoint_count"),
        field_name="auto_route_waypoint_count",
    )
    battery_remaining_percent = (
        _coordinate_float(
            value.get("battery_remaining_percent"),
            field_name="battery_remaining_percent",
            low=0.0,
            high=100.0,
        )
        if value.get("battery_remaining_percent") not in (None, "")
        else None
    )
    battery_scenario = str(value.get("battery_scenario") or "").strip().lower()
    if battery_scenario not in ("", "battery_low", "battery_critical"):
        raise ValueError("battery_scenario must be battery_low or battery_critical")
    if not battery_scenario and battery_remaining_percent is not None:
        battery_scenario = (
            "battery_critical" if battery_remaining_percent <= 10.0 else "battery_low"
        )
    sensor_failure_component = str(
        value.get("sensor_failure_component") or ""
    ).strip().lower()
    sensor_failure_type = str(value.get("sensor_failure_type") or "").strip().lower()
    if sensor_failure_type and not sensor_failure_component:
        sensor_failure_component = "gps"
    if sensor_failure_component not in ("", "gps"):
        raise ValueError("sensor_failure_component must be gps")
    if sensor_failure_type not in ("", "off"):
        raise ValueError("sensor_failure_type must be off for the current GPS failure slice")
    landing_zone_blocked_raw = value.get("landing_zone_blocked")
    landing_zone_blocked = (
        str(landing_zone_blocked_raw).strip().lower()
        in ("1", "true", "yes", "on", "blocked")
        if landing_zone_blocked_raw not in (None, "")
        else False
    )
    visibility_mode = str(value.get("visibility_mode") or "").strip().lower()
    if visibility_mode not in ("", "fog", "smoke"):
        raise ValueError("visibility_mode must be fog or smoke")
    no_fly_zone_marker_raw = value.get("no_fly_zone_marker")
    no_fly_zone_marker = (
        str(no_fly_zone_marker_raw).strip().lower()
        in ("1", "true", "yes", "on", "visual", "marker")
        if no_fly_zone_marker_raw not in (None, "")
        else False
    )
    traffic_conflict_marker_raw = value.get("traffic_conflict_marker")
    traffic_conflict_marker = (
        str(traffic_conflict_marker_raw).strip().lower()
        in ("1", "true", "yes", "on", "visual", "marker", "vehicle")
        if traffic_conflict_marker_raw not in (None, "")
        else False
    )
    alternate_landing_marker_raw = value.get("alternate_landing_marker")
    alternate_landing_marker = (
        str(alternate_landing_marker_raw).strip().lower()
        in ("1", "true", "yes", "on", "visual", "marker", "alternate")
        if alternate_landing_marker_raw not in (None, "")
        else False
    )
    moving_actor_marker_raw = value.get("moving_actor_marker")
    moving_actor_marker = (
        str(moving_actor_marker_raw).strip().lower()
        in ("1", "true", "yes", "on", "visual", "marker", "actor")
        if moving_actor_marker_raw not in (None, "")
        else False
    )
    multi_drone_conflict_probe_raw = value.get("multi_drone_conflict_probe")
    multi_drone_conflict_probe = (
        str(multi_drone_conflict_probe_raw).strip().lower()
        in ("1", "true", "yes", "on", "probe", "multidrone", "multi_drone")
        if multi_drone_conflict_probe_raw not in (None, "")
        else False
    )
    telemetry_dropout_mode = str(value.get("telemetry_dropout_mode") or "").strip().lower()
    telemetry_dropout_aliases = {
        "": "",
        "none": "",
        "off": "",
        "observer": "observer_sample_pause",
        "observer_side_dropout": "observer_sample_pause",
        "observer_pose_gap": "observer_sample_pause",
        "pose_gap": "observer_sample_pause",
        "observer_sample_pause": "observer_sample_pause",
        "sample_pause": "observer_sample_pause",
    }
    if telemetry_dropout_mode not in telemetry_dropout_aliases:
        raise ValueError(
            "telemetry_dropout_mode must be observer_sample_pause or empty"
        )
    telemetry_dropout_mode = telemetry_dropout_aliases[telemetry_dropout_mode]
    mavlink_link_degradation_mode = str(
        value.get("mavlink_link_degradation_mode") or ""
    ).strip().lower()
    mavlink_link_degradation_aliases = {
        "": "",
        "none": "",
        "off": "",
        "heartbeat": "heartbeat_observer",
        "heartbeat_observer": "heartbeat_observer",
        "heartbeat_gap_observer": "heartbeat_observer",
        "mavlink_heartbeat_observer": "heartbeat_observer",
        "link_loss": "link_loss_probe",
        "mavlink_link_loss": "link_loss_probe",
        "mavlink_link_loss_probe": "link_loss_probe",
        "link_loss_probe": "link_loss_probe",
    }
    if mavlink_link_degradation_mode not in mavlink_link_degradation_aliases:
        raise ValueError(
            "mavlink_link_degradation_mode must be heartbeat_observer, link_loss_probe, or empty"
        )
    mavlink_link_degradation_mode = mavlink_link_degradation_aliases[
        mavlink_link_degradation_mode
    ]
    distance_m = _haversine_distance_m(
        latitude_a=takeoff_latitude,
        longitude_a=takeoff_longitude,
        latitude_b=dropoff_latitude,
        longitude_b=dropoff_longitude,
    )
    bbox = _bbox_for_coordinate_pair(
        takeoff_latitude=takeoff_latitude,
        takeoff_longitude=takeoff_longitude,
        dropoff_latitude=dropoff_latitude,
        dropoff_longitude=dropoff_longitude,
    )
    route = {
        "schema_version": "mission_designer_coordinate_pair_route.v1",
        "route_mode": "operator_coordinate_pair",
        "takeoff_latitude": round(takeoff_latitude, 7),
        "takeoff_longitude": round(takeoff_longitude, 7),
        "dropoff_latitude": round(dropoff_latitude, 7),
        "dropoff_longitude": round(dropoff_longitude, 7),
        "dropoff_roof_height_agl_m": round(roof_height_agl_m, 3),
        "payload_weight_kg": payload_weight_kg,
        "wind_speed_mps": wind_speed_mps,
        "wind_direction_deg": wind_direction_deg,
        "wind_gust_mps": wind_gust_mps,
        "wind_variance": wind_variance,
        "temperature_c": temperature_c,
        "pressure_hpa": pressure_hpa,
        "precipitation_mm_per_hour": precipitation_mm_per_hour,
        "rain_visual_mode": str(value.get("rain_visual_mode") or "").strip().lower()
        or ("rain" if precipitation_mm_per_hour and precipitation_mm_per_hour > 0 else None),
        "rain_battery_drain_factor": rain_battery_drain_factor,
        "rain_sensor_degradation_factor": rain_sensor_degradation_factor,
        "rain_landing_risk_factor": rain_landing_risk_factor,
        "thermal_battery_drain_factor": thermal_battery_drain_factor,
        "thermal_motor_derate_factor": thermal_motor_derate_factor,
        "auto_route_waypoint_count": auto_route_waypoint_count,
        "battery_scenario": battery_scenario or None,
        "battery_remaining_percent": battery_remaining_percent,
        "sensor_failure_component": sensor_failure_component or None,
        "sensor_failure_type": sensor_failure_type or None,
        "landing_zone_blocked": landing_zone_blocked,
        "visibility_mode": visibility_mode or None,
        "no_fly_zone_marker": no_fly_zone_marker,
        "traffic_conflict_marker": traffic_conflict_marker,
        "alternate_landing_marker": alternate_landing_marker,
        "moving_actor_marker": moving_actor_marker,
        "multi_drone_conflict_probe": multi_drone_conflict_probe,
        "telemetry_dropout_mode": telemetry_dropout_mode or None,
        "mavlink_link_degradation_mode": mavlink_link_degradation_mode or None,
        "derived_route_distance_m": round(distance_m, 3),
        "derived_route_distance_km": round(distance_m / 1000.0, 6),
        "bbox": bbox,
        "source_url": "operator://mission-designer/coordinate-pair",
        "planning_only": True,
        "execution_binding_allowed": False,
        "gazebo_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
    }
    for label_field in (
        "takeoff_label",
        "dropoff_label",
        "route_source",
        "payload_weight_source",
        "payload_split_plan_ref",
        "payload_split_sortie_id",
    ):
        label_value = str(value.get(label_field) or "").strip()
        if label_value:
            route[label_field] = label_value
    for payload_split_float_field in (
        "requested_total_payload_weight_kg",
        "payload_weight_kg_operator_requested_total",
        "payload_split_planning_max_payload_weight_kg_per_drone",
    ):
        if value.get(payload_split_float_field) not in (None, ""):
            route[payload_split_float_field] = round(
                _coordinate_float(
                    value.get(payload_split_float_field),
                    field_name=payload_split_float_field,
                    low=0.0,
                    high=100.0,
                ),
                3,
            )
    for payload_split_int_field in (
        "payload_split_sortie_index",
        "payload_split_sortie_count",
    ):
        if value.get(payload_split_int_field) not in (None, ""):
            route[payload_split_int_field] = _coordinate_positive_int(
                value.get(payload_split_int_field),
                field_name=payload_split_int_field,
            )
    source_refs = value.get("source_refs")
    if isinstance(source_refs, Sequence) and not isinstance(source_refs, str | bytes):
        route["source_refs"] = [str(ref) for ref in source_refs if str(ref).strip()]
    if terrain_profile:
        route["terrain_profile"] = terrain_profile
    if terrain_clearance_agl_m is not None:
        route["terrain_clearance_agl_m"] = round(terrain_clearance_agl_m, 3)
    for terrain_ref_field in ("terrain_profile_source", "terrain_profile_ref"):
        terrain_ref_value = str(value.get(terrain_ref_field) or "").strip()
        if terrain_ref_value:
            route[terrain_ref_field] = terrain_ref_value
    digest = sha256(
        json.dumps(route, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    route["route_hash"] = digest
    route["route_id"] = f"mission_designer_coordinate_pair_route_{digest[:12]}"
    return route


def _coordinate_route_is_payload_split(route: Mapping[str, Any]) -> bool:
    return bool(route.get("payload_split_plan_ref")) or (
        str(route.get("payload_weight_source") or "") == "missionos_payload_split_plan"
    )


def _coordinate_route_prompt(prompt: str, route: Mapping[str, Any] | None) -> str:
    if not route:
        return prompt
    base_prompt = prompt
    if _coordinate_route_is_payload_split(route):
        base_prompt = "Payload split sortie mission for bounded Mission Designer route."
    payload = route.get("payload_weight_kg")
    wind_speed = route.get("wind_speed_mps")
    wind_direction = route.get("wind_direction_deg")
    wind_gust = route.get("wind_gust_mps")
    wind_variance = route.get("wind_variance")
    temperature = route.get("temperature_c")
    pressure = route.get("pressure_hpa")
    precipitation = route.get("precipitation_mm_per_hour")
    rain_visual_mode = route.get("rain_visual_mode")
    rain_battery_drain = route.get("rain_battery_drain_factor")
    rain_sensor_degradation = route.get("rain_sensor_degradation_factor")
    rain_landing_risk = route.get("rain_landing_risk_factor")
    thermal_battery_drain = route.get("thermal_battery_drain_factor")
    thermal_motor_derate = route.get("thermal_motor_derate_factor")
    auto_route_waypoint_count = route.get("auto_route_waypoint_count")
    battery_scenario = route.get("battery_scenario")
    battery_remaining = route.get("battery_remaining_percent")
    sensor_failure_component = route.get("sensor_failure_component")
    sensor_failure_type = route.get("sensor_failure_type")
    landing_zone_blocked = route.get("landing_zone_blocked")
    visibility_mode = route.get("visibility_mode")
    no_fly_zone_marker = route.get("no_fly_zone_marker")
    traffic_conflict_marker = route.get("traffic_conflict_marker")
    alternate_landing_marker = route.get("alternate_landing_marker")
    moving_actor_marker = route.get("moving_actor_marker")
    multi_drone_conflict_probe = route.get("multi_drone_conflict_probe")
    telemetry_dropout_mode = route.get("telemetry_dropout_mode")
    mavlink_link_degradation_mode = route.get("mavlink_link_degradation_mode")
    parts = [
        base_prompt,
        (
            " operator_coordinate_pair_route "
            f"{route['derived_route_distance_km']}km"
            f" {route['dropoff_roof_height_agl_m']}m roof_agl"
        ),
    ]
    if payload is not None:
        parts.append(f" payload {payload}kg")
    if wind_speed is not None:
        parts.append(f" wind_speed_mps={wind_speed}")
    if wind_direction is not None:
        parts.append(f" wind_direction_deg={wind_direction}")
    if wind_gust is not None:
        parts.append(f" wind_gust_mps={wind_gust}")
    if wind_variance is not None:
        parts.append(f" wind_variance={wind_variance}")
    if temperature is not None:
        parts.append(f" temperature_c={temperature}")
    if pressure is not None:
        parts.append(f" pressure_hpa={pressure}")
    if precipitation is not None:
        parts.append(f" precipitation_mm_per_hour={precipitation}")
    if rain_visual_mode:
        parts.append(f" rain_visual_mode={rain_visual_mode}")
    if rain_battery_drain is not None:
        parts.append(f" rain_battery_drain_factor={rain_battery_drain}")
    if rain_sensor_degradation is not None:
        parts.append(f" rain_sensor_degradation_factor={rain_sensor_degradation}")
    if rain_landing_risk is not None:
        parts.append(f" rain_landing_risk_factor={rain_landing_risk}")
    if thermal_battery_drain is not None:
        parts.append(f" thermal_battery_drain_factor={thermal_battery_drain}")
    if thermal_motor_derate is not None:
        parts.append(f" thermal_motor_derate_factor={thermal_motor_derate}")
    if auto_route_waypoint_count is not None:
        parts.append(f" auto_route_waypoint_count={auto_route_waypoint_count}")
    if battery_scenario:
        parts.append(f" battery_scenario={battery_scenario}")
    if battery_remaining is not None:
        parts.append(f" battery_remaining_percent={battery_remaining}")
    if sensor_failure_component:
        parts.append(f" sensor_failure_component={sensor_failure_component}")
    if sensor_failure_type:
        parts.append(f" sensor_failure_type={sensor_failure_type}")
    if landing_zone_blocked is True:
        parts.append(" landing_zone_blocked=true")
    if visibility_mode:
        parts.append(f" visibility_mode={visibility_mode}")
    if no_fly_zone_marker is True:
        parts.append(" no_fly_zone_marker=true")
    if traffic_conflict_marker is True:
        parts.append(" traffic_conflict_marker=true")
    if alternate_landing_marker is True:
        parts.append(" alternate_landing_marker=true")
    if moving_actor_marker is True:
        parts.append(" moving_actor_marker=true")
    if multi_drone_conflict_probe is True:
        parts.append(" multi_drone_conflict_probe=true")
    if telemetry_dropout_mode:
        parts.append(f" telemetry_dropout_mode={telemetry_dropout_mode}")
    if mavlink_link_degradation_mode:
        parts.append(
            f" mavlink_link_degradation_mode={mavlink_link_degradation_mode}"
        )
    return " ".join(parts)


def _coordinate_route_with_prompt_payload(
    prompt: str,
    route: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not route:
        return None
    if _coordinate_route_is_payload_split(route):
        updated = dict(route)
        prompt_payload_weight_kg = _extract_payload_weight_kg(prompt)
        if (
            prompt_payload_weight_kg is not None
            and "requested_total_payload_weight_kg" not in updated
        ):
            updated["requested_total_payload_weight_kg"] = prompt_payload_weight_kg
            updated["payload_weight_kg_operator_requested_total"] = prompt_payload_weight_kg
        return updated
    payload_weight_kg = _extract_payload_weight_kg(prompt)
    if payload_weight_kg is None:
        return dict(route)
    current_payload = route.get("payload_weight_kg")
    try:
        current_payload_float = (
            float(current_payload)
            if current_payload not in (None, "")
            else None
        )
    except (TypeError, ValueError):
        current_payload_float = None
    if (
        current_payload_float is not None
        and abs(current_payload_float - payload_weight_kg) <= 1e-9
    ):
        return dict(route)
    updated = _coordinate_route_from_payload(
        {
            **dict(route),
            "payload_weight_kg": payload_weight_kg,
        }
    )
    # Preserve the operator's structured route payload alongside the
    # chat-effective value. The hard envelope gate evaluates against the larger
    # of the two so a chat-mentioned weight can never silently lower a payload
    # past the contract envelope guard (while chat retry adjustments still drive
    # the effective payload_weight_kg).
    if updated is not None and current_payload_float is not None:
        updated["payload_weight_kg_operator_requested"] = current_payload_float
    return updated


def run_px4_gazebo_mission_scenario_designer(
    *,
    prompt: str,
    now: datetime | None = None,
    lesson_registry: Sequence[DeliveryMissionLesson | Mapping[str, Any]] | None = None,
    coordinate_route: Mapping[str, Any] | None = None,
    source_backed_dem_fetcher: Any | None = None,
) -> dict[str, Any]:
    coordinate_pair_route = _coordinate_route_from_payload(coordinate_route)
    coordinate_pair_route = _coordinate_route_with_prompt_payload(
        prompt,
        coordinate_pair_route,
    )
    request_prompt = _coordinate_route_prompt(prompt, coordinate_pair_route)
    request = build_px4_gazebo_mission_prompt_request(prompt=request_prompt, now=now)
    proposal = build_px4_gazebo_mission_scenario_proposal(
        prompt_request=request,
        lesson_registry=lesson_registry,
        now=now,
    )
    validation = build_px4_gazebo_mission_scenario_validation_result(
        prompt_request=request,
        proposal=proposal,
    )
    dry_run = build_px4_gazebo_mission_scenario_dry_run_result(
        prompt_request=request,
        proposal=proposal,
        validation=validation,
    )
    digital_twin_stage1 = build_digital_twin_stage1_environment(
        prompt=request.prompt,
        prompt_request_ref=_request_ref(request),
        altitude_target_m=proposal.altitude_target_m,
        payload_weight_kg=proposal.payload_weight_kg,
        weather_hazard_labels=proposal.weather_hazard_labels,
        source_backed_target_latitude=(
            coordinate_pair_route["dropoff_latitude"] if coordinate_pair_route else None
        ),
        source_backed_target_longitude=(
            coordinate_pair_route["dropoff_longitude"] if coordinate_pair_route else None
        ),
        source_backed_takeoff_latitude=(
            coordinate_pair_route["takeoff_latitude"] if coordinate_pair_route else None
        ),
        source_backed_takeoff_longitude=(
            coordinate_pair_route["takeoff_longitude"] if coordinate_pair_route else None
        ),
        source_backed_target_bbox=(
            coordinate_pair_route["bbox"] if coordinate_pair_route else None
        ),
        source_backed_dem_fetcher=source_backed_dem_fetcher,
        now=now,
    )
    execution_terrain_stage = digital_twin_stage1
    execution_terrain_fallback_reason = ""
    if coordinate_pair_route and not digital_twin_stage1.get("gazebo_world_artifact"):
        execution_terrain_stage = build_digital_twin_stage1_environment(
            prompt=request.prompt,
            prompt_request_ref=_request_ref(request),
            altitude_target_m=proposal.altitude_target_m,
            payload_weight_kg=proposal.payload_weight_kg,
            weather_hazard_labels=proposal.weather_hazard_labels,
            now=now,
        )
        execution_terrain_fallback_reason = (
            "source_backed_dem_unavailable_for_operator_coordinates;"
            "using_explicit_fixture_terrain_for_local_sitl"
        )
    return {
        "prompt_request": request.model_dump(mode="json"),
        "mission_designer_coordinate_pair_route": coordinate_pair_route,
        "scenario_proposal": proposal.model_dump(mode="json"),
        "validation_result": validation.model_dump(mode="json"),
        "dry_run_result": dry_run.model_dump(mode="json"),
        "real_world_mission_target": digital_twin_stage1[
            "real_world_mission_target"
        ],
        "real_world_target_resolution": digital_twin_stage1[
            "real_world_target_resolution"
        ],
        "real_world_geocode_candidate": digital_twin_stage1[
            "real_world_geocode_candidate"
        ],
        "terrain_dem_source_snapshot": digital_twin_stage1[
            "terrain_dem_source_snapshot"
        ],
        "terrain_dem_tile_request_candidate": digital_twin_stage1[
            "terrain_dem_tile_request_candidate"
        ],
        "terrain_dem_tile_snapshot": digital_twin_stage1[
            "terrain_dem_tile_snapshot"
        ],
        "tile_backed_terrain_environment_snapshot": digital_twin_stage1[
            "tile_backed_terrain_environment_snapshot"
        ],
        "terrain_heightmap_candidate": digital_twin_stage1[
            "terrain_heightmap_candidate"
        ],
        "terrain_heightmap_artifact": digital_twin_stage1[
            "terrain_heightmap_artifact"
        ],
        "terrain_heightmap_file_artifact": digital_twin_stage1[
            "terrain_heightmap_file_artifact"
        ]
        or execution_terrain_stage["terrain_heightmap_file_artifact"],
        "execution_terrain_fallback_reason": execution_terrain_fallback_reason,
        "execution_terrain_source_backed": (
            not bool(execution_terrain_fallback_reason)
        ),
        "gazebo_world_candidate": digital_twin_stage1[
            "gazebo_world_candidate"
        ]
        or execution_terrain_stage["gazebo_world_candidate"],
        "gazebo_world_artifact": digital_twin_stage1[
            "gazebo_world_artifact"
        ]
        or execution_terrain_stage["gazebo_world_artifact"],
        "coordinate_transform_candidate": digital_twin_stage1[
            "coordinate_transform_candidate"
        ]
        or execution_terrain_stage["coordinate_transform_candidate"],
        "digital_twin_mission_anchor_candidate": digital_twin_stage1[
            "digital_twin_mission_anchor_candidate"
        ],
        "digital_twin_px4_mission_item_candidate": digital_twin_stage1[
            "digital_twin_px4_mission_item_candidate"
        ]
        or execution_terrain_stage["digital_twin_px4_mission_item_candidate"],
        "digital_twin_sitl_binding_gate": digital_twin_stage1[
            "digital_twin_sitl_binding_gate"
        ]
        or execution_terrain_stage["digital_twin_sitl_binding_gate"],
        "terrain_environment_snapshot": digital_twin_stage1[
            "terrain_environment_snapshot"
        ],
        "weather_environment_snapshot": digital_twin_stage1[
            "weather_environment_snapshot"
        ],
        "digital_twin_route_feasibility": digital_twin_stage1[
            "digital_twin_route_feasibility"
        ],
        "weather_environment_policy_gate": digital_twin_stage1[
            "weather_environment_policy_gate"
        ],
        "digital_twin_route_plan": digital_twin_stage1["digital_twin_route_plan"]
        or execution_terrain_stage["digital_twin_route_plan"],
        "summary": {
            "validation_status": validation.validation_status.value,
            "dry_run_status": dry_run.dry_run_status.value,
            "weather_hazard_labels": list(proposal.weather_hazard_labels),
            "terrain_hazard_labels": list(proposal.terrain_hazard_labels),
            "equipment_incident_labels": list(proposal.equipment_incident_labels),
            "altitude_target_m": proposal.altitude_target_m,
            "payload_weight_kg": proposal.payload_weight_kg,
            "extracted_constraint_labels": list(proposal.extracted_constraint_labels),
            "feasibility_risk_labels": list(proposal.feasibility_risk_labels),
            "proposed_waypoint_count": proposal.proposed_waypoint_count,
            "proposed_route_segment_count": proposal.proposed_route_segment_count,
            "used_lesson_refs": list(proposal.used_lesson_refs),
            "ignored_lesson_refs": list(proposal.ignored_lesson_refs),
            "suppressed_scenario_candidates_count": len(
                proposal.suppressed_scenario_candidates
            ),
            "verifier_contract_ref": proposal.verifier_contract_ref,
            "lesson_registry_snapshot_hash": proposal.lesson_registry_snapshot_hash,
            "gazebo_execution_invoked": False,
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
            "llm_output_is_authority": False,
            "coordinate_pair_route_mode": bool(coordinate_pair_route),
            "coordinate_pair_route_ref": (
                "mission_designer_coordinate_pair_route:"
                + str(coordinate_pair_route["route_id"])
                if coordinate_pair_route
                else ""
            ),
            **digital_twin_stage1["summary"],
            "execution_terrain_fallback_reason": execution_terrain_fallback_reason,
            "execution_terrain_source_backed": (
                not bool(execution_terrain_fallback_reason)
            ),
        },
    }


__all__ = [
    "PX4_GAZEBO_BOUNDED_SIMULATION_REQUEST_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_DESIGNER_SITL_EXECUTION_REQUEST_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_SCENARIO_APPROVAL_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_SCENARIO_COMPILE_RESULT_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_PROMPT_REQUEST_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_SCENARIO_DRY_RUN_RESULT_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_SCENARIO_PROPOSAL_SCHEMA_VERSION",
    "PX4_GAZEBO_MISSION_SCENARIO_VALIDATION_RESULT_SCHEMA_VERSION",
    "DIGITAL_TWIN_ROUTE_FEASIBILITY_SCHEMA_VERSION",
    "DIGITAL_TWIN_ROUTE_PLAN_SCHEMA_VERSION",
    "REAL_WORLD_GEOCODE_CANDIDATE_SCHEMA_VERSION",
    "REAL_WORLD_MISSION_TARGET_SCHEMA_VERSION",
    "TERRAIN_DEM_TILE_REQUEST_CANDIDATE_SCHEMA_VERSION",
    "TERRAIN_DEM_TILE_SNAPSHOT_SCHEMA_VERSION",
    "TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION",
    "TILE_BACKED_TERRAIN_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION",
    "WEATHER_ENVIRONMENT_POLICY_GATE_SCHEMA_VERSION",
    "WEATHER_ENVIRONMENT_SNAPSHOT_SCHEMA_VERSION",
    "PX4GazeboBoundedSimulationRequest",
    "PX4GazeboMissionDesignerSITLExecutionRequest",
    "IgnoredLessonRecord",
    "PX4GazeboMissionPromptRequest",
    "PX4GazeboMissionScenarioDesignerError",
    "PX4GazeboMissionScenarioApproval",
    "PX4GazeboMissionScenarioCompileResult",
    "PX4GazeboMissionScenarioDryRunResult",
    "PX4GazeboMissionScenarioDryRunStatus",
    "PX4GazeboMissionScenarioProposal",
    "PX4GazeboMissionScenarioValidationResult",
    "PX4GazeboMissionScenarioValidationStatus",
    "SuppressedScenario",
    "approve_px4_gazebo_mission_scenario_for_bounded_simulation",
    "build_px4_gazebo_bounded_simulation_request",
    "build_px4_gazebo_mission_designer_sitl_execution_request",
    "build_px4_gazebo_mission_prompt_request",
    "build_px4_gazebo_mission_scenario_approval",
    "build_px4_gazebo_mission_scenario_compile_result",
    "build_px4_gazebo_mission_scenario_dry_run_result",
    "build_px4_gazebo_mission_scenario_proposal",
    "build_px4_gazebo_mission_scenario_validation_result",
    "run_px4_gazebo_mission_scenario_designer",
]
