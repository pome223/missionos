"""PX4 mission target runtime boundary.

Mission OS Core should talk to PX4 through this target interface instead of
branching on Gazebo-vs-physical details. The current implementation is a thin
Gazebo SITL adapter over the existing Digital Twin runtime helpers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class PX4MissionTargetError(RuntimeError):
    """Raised when a PX4 mission target backend is not wired for a command."""


MissionUploadReceipt = Any
ArmReceipt = Any
ModeReceipt = Any
MissionStartReceipt = Any
TargetTelemetry = Any
MissionEvent = Any
AbortReceipt = Any


@runtime_checkable
class PX4MissionTarget(Protocol):
    """Target-invariant PX4 mission runtime interface."""

    requires_gcs_heartbeat: bool

    def upload_mission(self, mission_plan: Any) -> MissionUploadReceipt: ...

    def arm(self) -> ArmReceipt: ...

    def set_mode(self, mode: str) -> ModeReceipt: ...

    def start_mission(self) -> MissionStartReceipt: ...

    def observe(self) -> TargetTelemetry: ...

    def collect_events(self) -> list[MissionEvent]: ...

    def stop_or_abort(self) -> AbortReceipt: ...


@dataclass(frozen=True)
class PX4GazeboBackend:
    """PX4/Gazebo SITL backend for the target-invariant PX4 interface."""

    upload_mission_handler: Callable[[Any], MissionUploadReceipt] | None = None
    arm_handler: Callable[[], ArmReceipt] | None = None
    set_mode_handler: Callable[[str], ModeReceipt] | None = None
    start_mission_handler: Callable[[], MissionStartReceipt] | None = None
    observe_handler: Callable[[], TargetTelemetry] | None = None
    collect_events_handler: Callable[[], list[MissionEvent]] | None = None
    stop_or_abort_handler: Callable[[], AbortReceipt] | None = None
    requires_gcs_heartbeat: bool = True

    def upload_mission(self, mission_plan: Any) -> MissionUploadReceipt:
        if self.upload_mission_handler is None:
            raise PX4MissionTargetError("PX4GazeboBackend upload handler missing")
        return self.upload_mission_handler(mission_plan)

    def arm(self) -> ArmReceipt:
        if self.arm_handler is None:
            raise PX4MissionTargetError("PX4GazeboBackend arm handler missing")
        return self.arm_handler()

    def set_mode(self, mode: str) -> ModeReceipt:
        if self.set_mode_handler is None:
            raise PX4MissionTargetError("PX4GazeboBackend set_mode handler missing")
        return self.set_mode_handler(mode)

    def start_mission(self) -> MissionStartReceipt:
        if self.start_mission_handler is None:
            raise PX4MissionTargetError("PX4GazeboBackend mission start handler missing")
        return self.start_mission_handler()

    def observe(self) -> TargetTelemetry:
        if self.observe_handler is None:
            raise PX4MissionTargetError("PX4GazeboBackend observe handler missing")
        return self.observe_handler()

    def collect_events(self) -> list[MissionEvent]:
        if self.collect_events_handler is None:
            return []
        return self.collect_events_handler()

    def stop_or_abort(self) -> AbortReceipt:
        if self.stop_or_abort_handler is None:
            return {"abort_attempted": False, "reason": "no_backend_abort_handler"}
        return self.stop_or_abort_handler()


__all__ = [
    "AbortReceipt",
    "ArmReceipt",
    "MissionEvent",
    "MissionStartReceipt",
    "MissionUploadReceipt",
    "ModeReceipt",
    "PX4GazeboBackend",
    "PX4MissionTarget",
    "PX4MissionTargetError",
    "TargetTelemetry",
]
