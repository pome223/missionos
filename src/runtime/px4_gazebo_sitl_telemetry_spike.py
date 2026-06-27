"""PX4/Gazebo-compatible fake-source telemetry-only spike artifacts.

This legacy spike is deliberately a compatibility fixture path, not actual
PX4/Gazebo SITL process evidence. Actual #408 SITL telemetry runs must use
`px4_gazebo_sitl_telemetry_run.v1` instead.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from src.runtime.task_store import TaskStore, get_task_store

PX4_GAZEBO_SITL_TELEMETRY_ONLY_SPIKE_SCHEMA_VERSION = (
    "px4_gazebo_sitl_telemetry_only_spike.v1"
)


class PX4GazeboSITLTelemetryOnlySpike(BaseModel):
    """Artifact for the current PX4/Gazebo-compatible telemetry-only spike.

    This is deliberately narrower than coupled PX4+Gazebo flight execution: it
    proves the telemetry-only artifact chain can consume a PX4/Gazebo-compatible
    source without granting MAVLink, ROS, actuator, hardware, or physical
    authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SITL_TELEMETRY_ONLY_SPIKE_SCHEMA_VERSION] = (
        PX4_GAZEBO_SITL_TELEMETRY_ONLY_SPIKE_SCHEMA_VERSION
    )
    spike_id: str
    spike_status: Literal["completed"] = "completed"
    spike_scope: Literal["px4_gazebo_compatible_telemetry_only"] = (
        "px4_gazebo_compatible_telemetry_only"
    )
    source_kind: str
    source_id: str
    telemetry_ref: str
    hil_review_ref: str
    gate_ref: str
    px4_gazebo_compatible_log_source_started: Literal[True] = True
    px4_gazebo_sitl_telemetry_only_spike: Literal[True] = True
    coupled_px4_gazebo_execution_invoked: Literal[False] = False
    actual_px4_gazebo_flight_control_invoked: Literal[False] = False
    gazebo_execution_invoked: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    px4_mission_upload_allowed: Literal[False] = False
    ros_dispatch_allowed: Literal[False] = False
    mavlink_dispatch_allowed: Literal[False] = False
    actuator_execution_allowed: Literal[False] = False
    unbounded_setpoint_stream_allowed: Literal[False] = False
    arbitrary_gazebo_mutation_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_refs(self) -> "PX4GazeboSITLTelemetryOnlySpike":
        if not self.telemetry_ref.startswith("px4_gazebo_sanitized_telemetry:"):
            raise ValueError("SITL telemetry spike requires telemetry ref")
        if not self.hil_review_ref.startswith("hil_telemetry_review:"):
            raise ValueError("SITL telemetry spike requires HIL review ref")
        if not self.gate_ref.startswith("autonomy_gate_result:"):
            raise ValueError("SITL telemetry spike requires gate ref")
        return self


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    digest = sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def build_px4_gazebo_sitl_telemetry_only_spike(
    *,
    sanitized_telemetry: dict[str, Any],
    hil_review: dict[str, Any],
    autonomy_gate_result: dict[str, Any],
) -> PX4GazeboSITLTelemetryOnlySpike:
    telemetry_id = sanitized_telemetry.get("telemetry_id")
    review_id = hil_review.get("review_id")
    gate_id = autonomy_gate_result.get("gate_id")
    payload = {
        "telemetry_id": telemetry_id,
        "review_id": review_id,
        "gate_id": gate_id,
        "source_kind": sanitized_telemetry.get("source_kind"),
        "source_id": sanitized_telemetry.get("source_id"),
    }
    return PX4GazeboSITLTelemetryOnlySpike(
        spike_id=_stable_id("px4_gazebo_sitl_telemetry_spike", payload),
        source_kind=str(sanitized_telemetry.get("source_kind") or ""),
        source_id=str(sanitized_telemetry.get("source_id") or ""),
        telemetry_ref=f"px4_gazebo_sanitized_telemetry:{telemetry_id}",
        hil_review_ref=f"hil_telemetry_review:{review_id}",
        gate_ref=f"autonomy_gate_result:{gate_id}",
    )


def attach_px4_gazebo_sitl_telemetry_only_spike(
    *,
    task_id: str,
    artifacts: dict[str, Any],
    task_store_factory=get_task_store,
) -> dict[str, Any]:
    spike = build_px4_gazebo_sitl_telemetry_only_spike(
        sanitized_telemetry=artifacts["px4_gazebo_sanitized_telemetry"],
        hil_review=artifacts["hil_telemetry_review"],
        autonomy_gate_result=artifacts["autonomy_gate_result"],
    )
    store: TaskStore = task_store_factory()
    updated = store.update(
        task_id,
        artifacts={
            "px4_gazebo_sitl_telemetry_only_spike": spike.model_dump(mode="json")
        },
    )
    if updated is None:
        raise RuntimeError(
            f"PX4/Gazebo SITL telemetry-only spike task not found: {task_id}"
        )
    return {
        **artifacts,
        "px4_gazebo_sitl_telemetry_only_spike": spike.model_dump(mode="json"),
        "task": updated,
    }


__all__ = [
    "PX4_GAZEBO_SITL_TELEMETRY_ONLY_SPIKE_SCHEMA_VERSION",
    "PX4GazeboSITLTelemetryOnlySpike",
    "attach_px4_gazebo_sitl_telemetry_only_spike",
    "build_px4_gazebo_sitl_telemetry_only_spike",
]
