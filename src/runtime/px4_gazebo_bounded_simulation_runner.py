"""Bounded Gazebo Sim runner for approved Mission Designer requests.

This module consumes the proposal/approval-derived
``px4_gazebo_bounded_simulation_request.v1`` artifact and turns an opt-in
bounded ``gz sim`` stdout window into sanitized telemetry, HIL review/gate
artifacts, and a bounded-run artifact. It does not send MAVLink, ROS, setpoint,
actuator, mission-upload, hardware, or physical-execution commands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.runtime.gz_sim_log_collector import (
    GzSimLogCollectorError,
    attach_gz_sim_log_hil_review_gate_artifacts,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    PX4GazeboBoundedSimulationRequest,
)
from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_BOUNDED_SIMULATION_RUN_SCHEMA_VERSION = (
    "px4_gazebo_bounded_simulation_run.v1"
)

_COMMAND_LIKE_STDOUT_RE = re.compile(
    r"(?i)"
    r"(COMMAND_LONG|MISSION_ITEM(?:_INT)?|MAV_CMD|MAVLink|/cmd_vel|/mavlink|"
    r"/fmu/|udp:|tcp:|ros\s+topic\s+pub|setpoint\s*[:=]|"
    r"\bport\s*[:=]?\s*\d{2,5}\b)"
)


class PX4GazeboBoundedSimulationRunnerError(RuntimeError):
    """Raised when a bounded Gazebo simulation run must fail closed."""


class PX4GazeboBoundedSimulationRun(BaseModel):
    """Artifact proving one bounded Gazebo Sim request execution window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_BOUNDED_SIMULATION_RUN_SCHEMA_VERSION] = (
        PX4_GAZEBO_BOUNDED_SIMULATION_RUN_SCHEMA_VERSION
    )
    run_id: str
    request_ref: str
    scenario_kind: Literal[
        "generic_bounded_delivery",
        "mountain_summit_payload_delivery",
    ]
    route_profile: Literal["standard_bounded_route", "staged_ascent_required"]
    scenario_run_mapping: Literal[
        "generic_bounded_delivery:standard_bounded_route->gz_sim_harmonic_minimal_world_telemetry_only",
        "mountain_summit_payload_delivery:staged_ascent_required->gz_sim_harmonic_minimal_world_telemetry_only",
    ]
    scenario_mapping_status: Literal["mapped_to_minimal_world_telemetry_only"] = (
        "mapped_to_minimal_world_telemetry_only"
    )
    scenario_specific_episode_invoked: Literal[False] = False
    request_runner_kind: Literal["deterministic_bounded_mission_runner"]
    executed_runner_kind: Literal["bounded_gz_sim_harmonic_runner"] = (
        "bounded_gz_sim_harmonic_runner"
    )
    status: Literal["completed", "failed", "blocked"]
    started_at: datetime
    finished_at: datetime
    max_duration_seconds: float = Field(gt=0)
    max_log_lines: int = Field(gt=0)
    observed_log_line_count: int = Field(ge=0)
    window_bounded: Literal[True] = True
    telemetry_captured_at: datetime
    max_telemetry_age_seconds: float = Field(gt=0)
    telemetry_age_seconds: float = Field(ge=0)
    source_image: str | None = None
    image_tag: str | None = None
    world_name: str
    world_ref: str
    world_sdf_path: str
    server_marker_observed: Literal[True] = True
    world_load_marker_observed: Literal[True] = True
    loaded_level_marker_observed: Literal[True] = True
    container_id: str | None = None
    container_exit_code: int | None = None
    network_mode: str | None = None
    port_bindings: dict[str, Any] = Field(default_factory=dict)
    read_only_rootfs: bool | None = None
    privileged: bool | None = None
    cap_drop: tuple[str, ...] = ()
    bounded_simulation_invoked: Literal[True] = True
    bounded_gazebo_runner_opt_in: Literal[True] = True
    gazebo_execution_invoked: Literal[True] = True
    deterministic_bounded_runner_invoked: Literal[False] = False
    general_gazebo_execution_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    setpoint_stream_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False
    approval_free_dispatch_allowed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    memory_direct_command_authority_allowed: Literal[False] = False
    telemetry_refs: tuple[str, ...]
    gate_ref: str
    hil_review_ref: str
    blocked_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_run(self) -> "PX4GazeboBoundedSimulationRun":
        if not self.request_ref.startswith("px4_gazebo_bounded_simulation_request:"):
            raise ValueError("bounded simulation run requires request ref")
        if self.status == "completed":
            if not self.telemetry_refs:
                raise ValueError(
                    "completed bounded simulation run requires telemetry refs"
                )
            if not self.gate_ref.startswith("autonomy_gate_result:"):
                raise ValueError("completed bounded simulation run requires gate ref")
            if not self.hil_review_ref.startswith("hil_telemetry_review:"):
                raise ValueError(
                    "completed bounded simulation run requires HIL review ref"
                )
            if self.blocked_reasons:
                raise ValueError("completed bounded simulation run cannot be blocked")
        if self.telemetry_age_seconds > self.max_telemetry_age_seconds:
            raise ValueError("bounded simulation run telemetry is stale")
        if self.network_mode not in (None, "none"):
            raise ValueError("bounded simulation run must use network_mode=none")
        if self.port_bindings:
            raise ValueError("bounded simulation run must not expose port bindings")
        if self.privileged is True:
            raise ValueError("bounded simulation run must not be privileged")
        return self


def _utc(value: datetime | None = None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        return resolved.replace(tzinfo=timezone.utc)
    return resolved.astimezone(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _request_ref(request: PX4GazeboBoundedSimulationRequest) -> str:
    return f"px4_gazebo_bounded_simulation_request:{request.request_id}"


def _artifact_ref(prefix: str, payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PX4GazeboBoundedSimulationRunnerError(
            f"bounded simulation missing artifact id: {key}"
        )
    return f"{prefix}:{value}"


def _coerce_request(
    request: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
) -> PX4GazeboBoundedSimulationRequest:
    if isinstance(request, PX4GazeboBoundedSimulationRequest):
        return request
    try:
        return PX4GazeboBoundedSimulationRequest.model_validate(request)
    except ValidationError as exc:
        raise PX4GazeboBoundedSimulationRunnerError(
            f"invalid bounded simulation request: {exc}"
        ) from exc


def _assert_approved_request(request: PX4GazeboBoundedSimulationRequest) -> None:
    if request.operator_approved is not True:
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo runner requires operator-approved request"
        )
    if request.approved_for_bounded_simulation is not True:
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo runner requires bounded simulation approval"
        )
    if request.approved_for_gazebo_execution is not False:
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo runner refuses general Gazebo execution approval"
        )
    if request.approval_scope != "compile_to_bounded_simulation_request_only":
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo runner requires compile-only approval scope"
        )


def _meaningful_line_count(log_text: str) -> int:
    return len([line for line in log_text.splitlines() if line.strip()])


def _scenario_run_mapping(
    request: PX4GazeboBoundedSimulationRequest,
) -> Literal[
    "generic_bounded_delivery:standard_bounded_route->gz_sim_harmonic_minimal_world_telemetry_only",
    "mountain_summit_payload_delivery:staged_ascent_required->gz_sim_harmonic_minimal_world_telemetry_only",
]:
    if (
        request.scenario_profile == "mountain_summit_payload_delivery"
        and request.route_profile == "staged_ascent_required"
    ):
        return (
            "mountain_summit_payload_delivery:staged_ascent_required->"
            "gz_sim_harmonic_minimal_world_telemetry_only"
        )
    return (
        "generic_bounded_delivery:standard_bounded_route->"
        "gz_sim_harmonic_minimal_world_telemetry_only"
    )


def _assert_fresh_telemetry(
    *,
    telemetry_captured_at: datetime,
    finished_at: datetime,
    max_telemetry_age_seconds: float,
) -> float:
    age = (finished_at - telemetry_captured_at).total_seconds()
    if age < 0:
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo telemetry timestamp is after run finish"
        )
    if age > max_telemetry_age_seconds:
        raise PX4GazeboBoundedSimulationRunnerError(
            f"bounded Gazebo telemetry is stale: {age}>{max_telemetry_age_seconds}"
        )
    return age


def _assert_safe_provenance(provenance: Mapping[str, Any] | None) -> None:
    if not provenance:
        return
    if provenance.get("network_mode") not in (None, "none"):
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo runner requires network_mode=none"
        )
    if provenance.get("port_bindings") not in (None, {}):
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo runner refuses exposed port bindings"
        )
    if provenance.get("privileged") is True:
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo runner refuses privileged containers"
        )
    if provenance.get("read_only_rootfs") is False:
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo runner requires read-only rootfs"
        )


def _provenance_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return ()


def _assert_bounded_stdout(
    log_text: str,
    *,
    max_log_lines: int,
    timed_out: bool,
) -> int:
    if timed_out:
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo simulation timed out before telemetry persistence"
        )
    if not log_text.strip():
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo simulation produced no stdout"
        )
    command_like = sorted(
        set(match.group(0) for match in _COMMAND_LIKE_STDOUT_RE.finditer(log_text))
    )
    if command_like:
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded Gazebo stdout contained command-like payload: "
            + ", ".join(command_like)
        )
    line_count = _meaningful_line_count(log_text)
    if line_count > max_log_lines:
        raise PX4GazeboBoundedSimulationRunnerError(
            f"bounded Gazebo stdout exceeded max_log_lines: {line_count}>{max_log_lines}"
        )
    return line_count


def build_px4_gazebo_bounded_simulation_run(
    *,
    request: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
    started_at: datetime,
    finished_at: datetime,
    max_duration_seconds: float,
    max_log_lines: int,
    observed_log_line_count: int,
    telemetry_captured_at: datetime,
    max_telemetry_age_seconds: float,
    telemetry_age_seconds: float,
    telemetry_refs: tuple[str, ...],
    gate_ref: str,
    hil_review_ref: str,
    provenance: Mapping[str, Any] | None = None,
    status: Literal["completed", "failed", "blocked"] = "completed",
    blocked_reasons: tuple[str, ...] = (),
) -> PX4GazeboBoundedSimulationRun:
    request_obj = _coerce_request(request)
    started = _utc(started_at)
    finished = _utc(finished_at)
    if finished < started:
        raise PX4GazeboBoundedSimulationRunnerError(
            "bounded simulation finished before it started"
        )
    duration = (finished - started).total_seconds()
    if duration > max_duration_seconds:
        raise PX4GazeboBoundedSimulationRunnerError(
            f"bounded simulation duration exceeded max_duration_seconds: {duration}"
        )
    payload = {
        "request_ref": _request_ref(request_obj),
        "scenario_kind": request_obj.scenario_profile,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "telemetry_refs": telemetry_refs,
        "gate_ref": gate_ref,
        "status": status,
    }
    return PX4GazeboBoundedSimulationRun(
        run_id=_stable_id("px4_gazebo_bounded_simulation_run", payload),
        request_ref=_request_ref(request_obj),
        scenario_kind=request_obj.scenario_profile,
        route_profile=request_obj.route_profile,
        scenario_run_mapping=_scenario_run_mapping(request_obj),
        request_runner_kind=request_obj.runner_kind,
        status=status,
        started_at=started,
        finished_at=finished,
        max_duration_seconds=max_duration_seconds,
        max_log_lines=max_log_lines,
        observed_log_line_count=observed_log_line_count,
        telemetry_captured_at=_utc(telemetry_captured_at),
        max_telemetry_age_seconds=max_telemetry_age_seconds,
        telemetry_age_seconds=telemetry_age_seconds,
        source_image=str((provenance or {}).get("source_image") or "") or None,
        image_tag=str((provenance or {}).get("image_tag") or "") or None,
        world_name=str((provenance or {}).get("world_name") or "empty"),
        world_ref=str((provenance or {}).get("world_ref") or "/tmp/empty.sdf"),
        world_sdf_path=str(
            (provenance or {}).get("world_sdf_path") or "/tmp/empty.sdf"
        ),
        container_id=str((provenance or {}).get("container_id") or "") or None,
        container_exit_code=(
            int((provenance or {}).get("container_exit_code"))
            if (provenance or {}).get("container_exit_code") is not None
            else None
        ),
        network_mode=str((provenance or {}).get("network_mode") or "") or None,
        port_bindings=dict((provenance or {}).get("port_bindings") or {}),
        read_only_rootfs=(provenance or {}).get("read_only_rootfs"),
        privileged=(provenance or {}).get("privileged"),
        cap_drop=_provenance_tuple((provenance or {}).get("cap_drop")),
        telemetry_refs=telemetry_refs,
        gate_ref=gate_ref,
        hil_review_ref=hil_review_ref,
        blocked_reasons=blocked_reasons,
    )


def run_px4_gazebo_bounded_simulation_request(
    *,
    task_id: str,
    request: PX4GazeboBoundedSimulationRequest | Mapping[str, Any],
    log_text: str,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    max_duration_seconds: float = 120.0,
    max_log_lines: int = 260,
    max_telemetry_age_seconds: float = 60.0,
    telemetry_captured_at: datetime | None = None,
    timed_out: bool = False,
    provenance: Mapping[str, Any] | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Attach HIL/gate artifacts and a bounded-run artifact to a task.

    Any invalid request, timeout, empty output, missing Gazebo markers, or
    command-like stdout raises before persistence so failure paths do not pollute
    the task with HIL/review/gate artifacts.
    """

    request_obj = _coerce_request(request)
    _assert_approved_request(request_obj)
    started = _utc(started_at)
    finished = _utc(finished_at)
    telemetry_captured = _utc(telemetry_captured_at or finished)
    telemetry_age_seconds = _assert_fresh_telemetry(
        telemetry_captured_at=telemetry_captured,
        finished_at=finished,
        max_telemetry_age_seconds=max_telemetry_age_seconds,
    )
    _assert_safe_provenance(provenance)
    observed_log_line_count = _assert_bounded_stdout(
        log_text,
        max_log_lines=max_log_lines,
        timed_out=timed_out,
    )
    store_factory = task_store_factory or get_task_store
    try:
        attached = attach_gz_sim_log_hil_review_gate_artifacts(
            task_id,
            log_text,
            captured_at=telemetry_captured,
            provenance=dict(provenance or {}),
            task_store_factory=store_factory,
        )
    except GzSimLogCollectorError as exc:
        raise PX4GazeboBoundedSimulationRunnerError(
            f"bounded Gazebo simulation telemetry rejected: {exc}"
        ) from exc
    telemetry = attached["px4_gazebo_sanitized_telemetry"]
    gate = attached["autonomy_gate_result"]
    review = attached["hil_telemetry_review"]
    run = build_px4_gazebo_bounded_simulation_run(
        request=request_obj,
        started_at=started,
        finished_at=finished,
        max_duration_seconds=max_duration_seconds,
        max_log_lines=max_log_lines,
        observed_log_line_count=observed_log_line_count,
        telemetry_captured_at=telemetry_captured,
        max_telemetry_age_seconds=max_telemetry_age_seconds,
        telemetry_age_seconds=telemetry_age_seconds,
        telemetry_refs=(
            _artifact_ref(
                "px4_gazebo_sanitized_telemetry",
                telemetry,
                "telemetry_id",
            ),
        ),
        gate_ref=_artifact_ref("autonomy_gate_result", gate, "gate_id"),
        hil_review_ref=_artifact_ref("hil_telemetry_review", review, "review_id"),
        provenance=provenance,
    )
    store = store_factory()
    updated = store.update(
        task_id,
        artifacts={"px4_gazebo_bounded_simulation_run": run.model_dump(mode="json")},
    )
    if updated is None:
        raise PX4GazeboBoundedSimulationRunnerError(
            f"bounded simulation task not found after attach: {task_id}"
        )
    return {
        **attached,
        "px4_gazebo_bounded_simulation_run": run.model_dump(mode="json"),
        "task": updated,
    }


__all__ = [
    "PX4_GAZEBO_BOUNDED_SIMULATION_RUN_SCHEMA_VERSION",
    "PX4GazeboBoundedSimulationRun",
    "PX4GazeboBoundedSimulationRunnerError",
    "build_px4_gazebo_bounded_simulation_run",
    "run_px4_gazebo_bounded_simulation_request",
]
