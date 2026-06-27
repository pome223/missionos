"""Read-only PX4/Gazebo delivery state correlation and readiness artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from src.runtime.gz_sim_log_collector import parse_gz_sim_entity_pose
from src.runtime.px4_gazebo_delivery_world_profile import (
    PX4GazeboDeliveryWorldProfile,
)
from src.runtime.px4_sitl_delivery_observation import PX4SitlDeliveryObservation
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_DELIVERY_STATE_CORRELATION_SCHEMA_VERSION = (
    "px4_gazebo_delivery_state_correlation.v1"
)
PX4_SITL_DELIVERY_READINESS_DIAGNOSTICS_SCHEMA_VERSION = (
    "px4_sitl_delivery_readiness_diagnostics.v1"
)
DEFAULT_GAZEBO_DELIVERY_ENTITY_NAME = "delivery_vehicle_state"
DEFAULT_GAZEBO_POSE_TOPIC = "/world/delivery_state_driven/pose/info"
COUPLED_DELIVERY_PHASES = ("pickup", "enroute", "dropoff", "completed")
COUPLED_DELIVERY_RUNNER_REF_PREFIX = "px4_gazebo_coupled_delivery_runner_result:"
COUPLED_DELIVERY_PHASE_EVIDENCE_REF_PREFIX = (
    "px4_gazebo_coupled_delivery_phase_evidence:"
)

ReadinessStatus = Literal["ready", "blocked"]


class PX4GazeboDeliveryStateCorrelationError(RuntimeError):
    """Raised when PX4/Gazebo state correlation evidence is unsafe."""


_FORBIDDEN_COMMAND_KEYS = frozenset(
    {
        "action",
        "actuator",
        "command",
        "dispatch",
        "entity_mutation",
        "gazebo_command",
        "gazebo_mutation",
        "hardware_target",
        "mavlink_command",
        "mission_upload",
        "physical_execution",
        "position_setpoint",
        "ros_action",
        "setpoint",
        "thrust",
        "torque",
    }
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


_FORBIDDEN_COMMAND_KEYS_NORMALIZED = frozenset(
    _normalize_key(key) for key in _FORBIDDEN_COMMAND_KEYS
)


def _command_like_key_paths(value: Any, *, root: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            key_text = str(key)
            path = f"{root}.{key_text}" if root else key_text
            if _normalize_key(key_text) in _FORBIDDEN_COMMAND_KEYS_NORMALIZED:
                findings.append(path)
            findings.extend(_command_like_key_paths(sub, root=path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{root}.{index}" if root else str(index)
            findings.extend(_command_like_key_paths(item, root=path))
    return findings


def _raise_for_command_like_keys(value: Any, *, root: str) -> None:
    findings = _command_like_key_paths(value, root=root)
    if findings:
        raise PX4GazeboDeliveryStateCorrelationError(
            "px4 gazebo delivery state correlation refused command-like keys: "
            + ", ".join(sorted(findings))
        )


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


def _as_profile(
    value: PX4GazeboDeliveryWorldProfile | Mapping[str, Any],
) -> PX4GazeboDeliveryWorldProfile:
    if isinstance(value, PX4GazeboDeliveryWorldProfile):
        return value
    return PX4GazeboDeliveryWorldProfile.model_validate(dict(value))


def _as_observation(
    value: PX4SitlDeliveryObservation | Mapping[str, Any],
) -> PX4SitlDeliveryObservation:
    if isinstance(value, PX4SitlDeliveryObservation):
        return value
    return PX4SitlDeliveryObservation.model_validate(dict(value))


def _profile_ref(profile: PX4GazeboDeliveryWorldProfile) -> str:
    return f"px4_gazebo_delivery_world_profile:{profile.profile_id}"


def _observation_ref(observation: PX4SitlDeliveryObservation) -> str:
    return f"px4_sitl_delivery_observation:{observation.observation_id}"


class _ReadOnlySafetyBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    gazebo_only: Literal[True] = True
    telemetry_only: Literal[True] = True
    read_only: Literal[True] = True
    command_surface_present: Literal[False] = False
    command_payload_allowed: Literal[False] = False
    dispatch_implementation_present: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    gazebo_entity_mutation_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    live_execution_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False


class PX4GazeboDeliveryStateCorrelation(_ReadOnlySafetyBoundary):
    """Correlation between PX4 SITL observation and Gazebo delivery vehicle pose."""

    schema_version: Literal[PX4_GAZEBO_DELIVERY_STATE_CORRELATION_SCHEMA_VERSION] = (
        PX4_GAZEBO_DELIVERY_STATE_CORRELATION_SCHEMA_VERSION
    )
    correlation_id: str
    profile_ref: str = Field(min_length=1)
    observation_ref: str = Field(min_length=1)
    telemetry_ref: str = Field(min_length=1)
    delivery_vehicle_ref: str = Field(min_length=1)
    px4_vehicle_id: str = Field(min_length=1)
    px4_sitl_model: str = Field(min_length=1)
    px4_sitl_image: str = Field(min_length=1)
    px4_sitl_started: bool
    gazebo_world_name: str = Field(min_length=1)
    gazebo_entity_name: str = Field(min_length=1)
    gazebo_pose_topic: str = Field(min_length=1)
    gazebo_pose_observed: bool
    gazebo_pose_x_m: float | None = None
    gazebo_pose_y_m: float | None = None
    gazebo_pose_z_m: float | None = None
    coupled_delivery_runner_ref: str | None = None
    coupled_delivery_phase_evidence_refs: tuple[str, ...] = ()
    observed_delivery_phases: tuple[str, ...] = ()
    coupled_motion_confirmed: bool = False
    state_correlation_status: ReadinessStatus
    blocked_reasons: tuple[str, ...] = ()
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @field_validator(
        "blocked_reasons",
        "coupled_delivery_phase_evidence_refs",
        "observed_delivery_phases",
        mode="before",
    )
    @classmethod
    def _normalize_text_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @model_validator(mode="after")
    def _validate_correlation(self) -> "PX4GazeboDeliveryStateCorrelation":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        if self.state_correlation_status == "ready" and self.blocked_reasons:
            raise ValueError("ready correlation cannot include blocked reasons")
        if self.state_correlation_status == "blocked" and not self.blocked_reasons:
            raise ValueError("blocked correlation requires blocked reasons")
        if self.gazebo_pose_observed:
            if (
                self.gazebo_pose_x_m is None
                or self.gazebo_pose_y_m is None
                or self.gazebo_pose_z_m is None
            ):
                raise ValueError("observed Gazebo pose requires x/y/z values")
        elif self.state_correlation_status == "ready":
            raise ValueError("ready correlation requires a Gazebo pose observation")
        if self.px4_sitl_started is not True and "px4_sitl_not_started" not in set(
            self.blocked_reasons
        ):
            raise ValueError("not-started PX4 SITL correlation must be blocked")
        has_coupled_refs = (
            self.coupled_delivery_runner_ref is not None
            or bool(self.coupled_delivery_phase_evidence_refs)
            or bool(self.observed_delivery_phases)
        )
        if has_coupled_refs and not self.coupled_motion_confirmed:
            raise ValueError(
                "coupled delivery refs require coupled_motion_confirmed=true"
            )
        if self.coupled_motion_confirmed:
            if not self.coupled_delivery_runner_ref:
                raise ValueError("coupled motion confirmation requires runner ref")
            if not self.coupled_delivery_runner_ref.startswith(
                COUPLED_DELIVERY_RUNNER_REF_PREFIX
            ):
                raise ValueError("coupled delivery runner ref has invalid prefix")
            if len(self.coupled_delivery_phase_evidence_refs) != len(
                COUPLED_DELIVERY_PHASES
            ):
                raise ValueError(
                    "coupled motion confirmation requires all delivery phase refs"
                )
            bad_refs = [
                ref
                for ref in self.coupled_delivery_phase_evidence_refs
                if not ref.startswith(COUPLED_DELIVERY_PHASE_EVIDENCE_REF_PREFIX)
            ]
            if bad_refs:
                raise ValueError(
                    "coupled delivery phase evidence ref has invalid prefix"
                )
            if set(self.observed_delivery_phases) != set(COUPLED_DELIVERY_PHASES):
                raise ValueError(
                    "coupled motion confirmation requires pickup/enroute/dropoff/"
                    "completed phases"
                )
        return self


class PX4SITLDeliveryReadinessDiagnostics(_ReadOnlySafetyBoundary):
    """Read-only readiness diagnostics derived from delivery state correlation."""

    schema_version: Literal[PX4_SITL_DELIVERY_READINESS_DIAGNOSTICS_SCHEMA_VERSION] = (
        PX4_SITL_DELIVERY_READINESS_DIAGNOSTICS_SCHEMA_VERSION
    )
    diagnostics_id: str
    correlation_ref: str = Field(min_length=1)
    readiness_status: ReadinessStatus
    ready_reasons: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    checked_at: datetime
    retry_attempted: Literal[False] = False
    stronger_execution_attempted: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("checked_at", mode="before")
    @classmethod
    def _coerce_checked_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _utc(parsed)

    @field_validator("ready_reasons", "blocked_reasons", mode="before")
    @classmethod
    def _normalize_reason_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))

    @model_validator(mode="after")
    def _validate_readiness(self) -> "PX4SITLDeliveryReadinessDiagnostics":
        _raise_for_command_like_keys(self.metadata, root="metadata")
        if self.readiness_status == "ready":
            if self.blocked_reasons:
                raise ValueError("ready diagnostics cannot include blocked reasons")
            if not self.ready_reasons:
                raise ValueError("ready diagnostics require ready reasons")
        elif not self.blocked_reasons:
            raise ValueError("blocked diagnostics require blocked reasons")
        return self


def build_px4_gazebo_delivery_state_correlation(
    *,
    profile: PX4GazeboDeliveryWorldProfile | Mapping[str, Any],
    observation: PX4SitlDeliveryObservation | Mapping[str, Any],
    gazebo_pose: Mapping[str, Any] | None = None,
    gazebo_pose_text: str | None = None,
    gazebo_entity_name: str = DEFAULT_GAZEBO_DELIVERY_ENTITY_NAME,
    gazebo_pose_topic: str = DEFAULT_GAZEBO_POSE_TOPIC,
    expected_delivery_vehicle_ref: str | None = None,
    coupled_delivery_runner_ref: str | None = None,
    coupled_delivery_phase_evidence_refs: tuple[str, ...] | list[str] | None = None,
    observed_delivery_phases: tuple[str, ...] | list[str] | None = None,
    coupled_motion_confirmed: bool = False,
    observed_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4GazeboDeliveryStateCorrelation:
    """Build a read-only state correlation artifact from profile/observation/pose."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    resolved_profile = _as_profile(profile)
    resolved_observation = _as_observation(observation)
    if resolved_observation.profile_ref != _profile_ref(resolved_profile):
        raise PX4GazeboDeliveryStateCorrelationError(
            "delivery state correlation profile/observation mismatch"
        )
    expected_ref = (
        expected_delivery_vehicle_ref or resolved_observation.delivery_vehicle_ref
    )
    if resolved_observation.delivery_vehicle_ref != expected_ref:
        raise PX4GazeboDeliveryStateCorrelationError(
            "delivery state correlation vehicle identity mismatch"
        )
    if (
        not resolved_observation.read_only
        or resolved_observation.command_surface_present
    ):
        raise PX4GazeboDeliveryStateCorrelationError(
            "delivery observation must be read-only and command-free"
        )
    if (
        resolved_profile.command_surface_present
        or resolved_profile.mavlink_dispatch_allowed
    ):
        raise PX4GazeboDeliveryStateCorrelationError(
            "delivery profile must be command-free for state correlation"
        )

    pose_payload: Mapping[str, Any] | None = gazebo_pose
    if pose_payload is None and gazebo_pose_text is not None:
        pose_payload = parse_gz_sim_entity_pose(
            gazebo_pose_text,
            entity_name=gazebo_entity_name,
        )

    blocked: list[str] = []
    px4_started = resolved_observation.measurements.get("px4_sitl_started") is True
    if not px4_started:
        blocked.append("px4_sitl_not_started")

    pose_observed = pose_payload is not None
    pose_x: float | None = None
    pose_y: float | None = None
    pose_z: float | None = None
    if pose_payload is None:
        blocked.append("gazebo_pose_missing")
    else:
        observed_entity = str(pose_payload.get("entity_name", "")).strip()
        if observed_entity != gazebo_entity_name:
            raise PX4GazeboDeliveryStateCorrelationError(
                "delivery state correlation Gazebo entity mismatch"
            )
        pose_x = float(pose_payload["x"])
        pose_y = float(pose_payload["y"])
        pose_z = float(pose_payload["z"])

    status: ReadinessStatus = "blocked" if blocked else "ready"
    captured = _utc(observed_at or resolved_observation.observed_at)
    payload = {
        "profile_id": resolved_profile.profile_id,
        "observation_id": resolved_observation.observation_id,
        "vehicle_ref": resolved_observation.delivery_vehicle_ref,
        "entity_name": gazebo_entity_name,
        "pose_topic": gazebo_pose_topic,
        "pose": dict(pose_payload or {}),
        "coupled_delivery_runner_ref": coupled_delivery_runner_ref,
        "coupled_delivery_phase_evidence_refs": tuple(
            coupled_delivery_phase_evidence_refs or ()
        ),
        "observed_delivery_phases": tuple(observed_delivery_phases or ()),
        "coupled_motion_confirmed": coupled_motion_confirmed,
        "status": status,
        "blocked": sorted(blocked),
    }
    try:
        return PX4GazeboDeliveryStateCorrelation(
            correlation_id=_stable_id("px4_gazebo_delivery_state_correlation", payload),
            profile_ref=_profile_ref(resolved_profile),
            observation_ref=_observation_ref(resolved_observation),
            telemetry_ref=resolved_observation.telemetry_ref,
            delivery_vehicle_ref=resolved_observation.delivery_vehicle_ref,
            px4_vehicle_id=resolved_observation.vehicle_id,
            px4_sitl_model=resolved_profile.px4_sitl_model,
            px4_sitl_image=resolved_profile.px4_sitl_image,
            px4_sitl_started=px4_started,
            gazebo_world_name=resolved_profile.gazebo_world_name,
            gazebo_entity_name=gazebo_entity_name,
            gazebo_pose_topic=gazebo_pose_topic,
            gazebo_pose_observed=pose_observed,
            gazebo_pose_x_m=pose_x,
            gazebo_pose_y_m=pose_y,
            gazebo_pose_z_m=pose_z,
            coupled_delivery_runner_ref=coupled_delivery_runner_ref,
            coupled_delivery_phase_evidence_refs=tuple(
                coupled_delivery_phase_evidence_refs or ()
            ),
            observed_delivery_phases=tuple(observed_delivery_phases or ()),
            coupled_motion_confirmed=coupled_motion_confirmed,
            state_correlation_status=status,
            blocked_reasons=tuple(blocked),
            observed_at=captured,
            metadata={
                **metadata_payload,
                "artifact_only": True,
                "issue": 322,
                "parent_epic": 307,
                "no_command_dispatch": True,
                "no_hardware_target": True,
            },
        )
    except ValidationError as exc:
        raise PX4GazeboDeliveryStateCorrelationError(
            f"invalid PX4/Gazebo delivery state correlation: {exc}"
        ) from exc


def build_px4_sitl_delivery_readiness_diagnostics(
    *,
    state_correlation: PX4GazeboDeliveryStateCorrelation | Mapping[str, Any],
    checked_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> PX4SITLDeliveryReadinessDiagnostics:
    """Build read-only readiness diagnostics from state correlation."""

    metadata_payload = dict(metadata or {})
    _raise_for_command_like_keys(metadata_payload, root="metadata")
    correlation = (
        state_correlation
        if isinstance(state_correlation, PX4GazeboDeliveryStateCorrelation)
        else PX4GazeboDeliveryStateCorrelation.model_validate(dict(state_correlation))
    )
    blocked = list(correlation.blocked_reasons)
    if correlation.state_correlation_status != "ready":
        blocked.append("state_correlation_not_ready")
    if correlation.px4_sitl_started is not True:
        blocked.append("px4_sitl_not_started")
    if correlation.gazebo_pose_observed is not True:
        blocked.append("gazebo_pose_missing")

    status: ReadinessStatus = "blocked" if blocked else "ready"
    if status == "ready":
        ready_reasons = [
            "profile_observation_correlated",
            "px4_sitl_started",
            "gazebo_delivery_vehicle_pose_observed",
            "command_surface_closed",
        ]
        if correlation.coupled_motion_confirmed:
            ready_reasons.append("coupled_delivery_motion_confirmed")
        ready = tuple(ready_reasons)
    else:
        ready = ()
    payload = {
        "correlation_id": correlation.correlation_id,
        "status": status,
        "blocked": sorted(set(blocked)),
        "checked_at": _utc(checked_at or correlation.observed_at).isoformat(),
    }
    return PX4SITLDeliveryReadinessDiagnostics(
        diagnostics_id=_stable_id("px4_sitl_delivery_readiness_diagnostics", payload),
        correlation_ref=(
            "px4_gazebo_delivery_state_correlation:" f"{correlation.correlation_id}"
        ),
        readiness_status=status,
        ready_reasons=ready,
        blocked_reasons=tuple(blocked),
        checked_at=_utc(checked_at or correlation.observed_at),
        metadata={
            **metadata_payload,
            "artifact_only": True,
            "issue": 323,
            "parent_epic": 307,
            "no_command_dispatch": True,
            "no_hardware_target": True,
        },
    )


def attach_px4_gazebo_delivery_state_readiness_artifacts(
    task_id: str,
    *,
    profile: PX4GazeboDeliveryWorldProfile | Mapping[str, Any],
    observation: PX4SitlDeliveryObservation | Mapping[str, Any],
    gazebo_pose: Mapping[str, Any] | None = None,
    gazebo_pose_text: str | None = None,
    gazebo_entity_name: str = DEFAULT_GAZEBO_DELIVERY_ENTITY_NAME,
    checked_at: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach state correlation/readiness and block the task if not ready."""

    store: TaskStore = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboDeliveryStateCorrelationError(
            f"task {task_id} not found; cannot attach PX4/Gazebo readiness"
        )
    correlation = build_px4_gazebo_delivery_state_correlation(
        profile=profile,
        observation=observation,
        gazebo_pose=gazebo_pose,
        gazebo_pose_text=gazebo_pose_text,
        gazebo_entity_name=gazebo_entity_name,
        observed_at=checked_at,
    )
    readiness = build_px4_sitl_delivery_readiness_diagnostics(
        state_correlation=correlation,
        checked_at=checked_at,
    )
    status = "running" if readiness.readiness_status == "ready" else "blocked"
    artifacts = {
        "px4_gazebo_delivery_state_correlation": correlation.model_dump(mode="json"),
        "px4_sitl_delivery_readiness_diagnostics": readiness.model_dump(mode="json"),
    }
    updated = store.update(task_id, status=status, artifacts=artifacts)
    if updated is None:
        raise PX4GazeboDeliveryStateCorrelationError(
            f"task {task_id} disappeared while attaching PX4/Gazebo readiness"
        )
    return artifacts


__all__ = [
    "DEFAULT_GAZEBO_DELIVERY_ENTITY_NAME",
    "DEFAULT_GAZEBO_POSE_TOPIC",
    "PX4_GAZEBO_DELIVERY_STATE_CORRELATION_SCHEMA_VERSION",
    "PX4_SITL_DELIVERY_READINESS_DIAGNOSTICS_SCHEMA_VERSION",
    "PX4GazeboDeliveryStateCorrelation",
    "PX4GazeboDeliveryStateCorrelationError",
    "PX4SITLDeliveryReadinessDiagnostics",
    "attach_px4_gazebo_delivery_state_readiness_artifacts",
    "build_px4_gazebo_delivery_state_correlation",
    "build_px4_sitl_delivery_readiness_diagnostics",
]
