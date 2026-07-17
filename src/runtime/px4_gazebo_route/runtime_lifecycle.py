"""Process and initial-readback lifecycle for the PX4/Gazebo route harness.

This boundary owns an isolated runtime directory and deterministic teardown.
It invokes caller-supplied simulator and readback callbacks but does not create
mission approval, dispatch authority, progress, completion, or physical-
execution claims.  A failed startup still attempts process cleanup.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PX4RouteRuntimeLifecycle:
    create_run_directory: Callable[[], Path]
    reset_runtime_state: Callable[[Path], Path]
    start_container: Callable[[Path], Path | None]
    wait_for_home: Callable[[], None]
    initialize_realism: Callable[[Path | None], Mapping[str, Any]]
    stop_container: Callable[[], None]
    mark_cleanup_observed: Callable[[Path], bool]


@dataclass(frozen=True)
class PX4RouteInitialRealismRuntime:
    terrain_world_readback: Callable[[Path | None], Mapping[str, Any]]
    apply_wind_realism: Callable[[Path | None], Mapping[str, Any]]
    thermal_weather_realism: Callable[[], Mapping[str, Any]]
    vehicle_realism: Callable[[Path | None], Mapping[str, Any]]
    battery_realism: Callable[[], Mapping[str, Any]]
    sensor_realism: Callable[[], Mapping[str, Any]]
    world_realism: Callable[[Path | None], Mapping[str, Any]]
    visibility_realism: Callable[[Path | None], Mapping[str, Any]]
    operational_realism: Callable[[Path | None], Mapping[str, Any]]
    mavlink_link_realism: Callable[[], Mapping[str, Any]]


@dataclass(frozen=True)
class PX4RouteInitialRealism:
    terrain_world: dict[str, Any]
    wind: dict[str, Any]
    thermal_weather: dict[str, Any]
    vehicle: dict[str, Any]
    battery: dict[str, Any]
    sensor: dict[str, Any]
    world: dict[str, Any]
    visibility: dict[str, Any]
    operational: dict[str, Any]
    mavlink_link: dict[str, Any]

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {
            name: dict(getattr(self, name))
            for name in (
                "terrain_world",
                "wind",
                "thermal_weather",
                "vehicle",
                "battery",
                "sensor",
                "world",
                "visibility",
                "operational",
                "mavlink_link",
            )
        }


@dataclass(frozen=True)
class PX4RouteRuntimeSession:
    run_dir: Path
    pose_trace_path: Path
    payload_model_root: Path | None
    initial_realism: dict[str, Any]


def collect_initial_realism(
    runtime: PX4RouteInitialRealismRuntime,
    *,
    payload_model_root: Path | None,
) -> PX4RouteInitialRealism:
    """Collect source-specific readbacks without promoting them to authority."""

    return PX4RouteInitialRealism(
        terrain_world=dict(runtime.terrain_world_readback(payload_model_root)),
        wind=dict(runtime.apply_wind_realism(payload_model_root)),
        thermal_weather=dict(runtime.thermal_weather_realism()),
        vehicle=dict(runtime.vehicle_realism(payload_model_root)),
        battery=dict(runtime.battery_realism()),
        sensor=dict(runtime.sensor_realism()),
        world=dict(runtime.world_realism(payload_model_root)),
        visibility=dict(runtime.visibility_realism(payload_model_root)),
        operational=dict(runtime.operational_realism(payload_model_root)),
        mavlink_link=dict(runtime.mavlink_link_realism()),
    )


@contextmanager
def px4_route_runtime_session(
    runtime: PX4RouteRuntimeLifecycle,
) -> Iterator[PX4RouteRuntimeSession]:
    """Start, initialize, yield, and always tear down one isolated SITL run."""

    run_dir = runtime.create_run_directory()
    payload_model_root: Path | None = None
    try:
        pose_trace_path = runtime.reset_runtime_state(run_dir)
        payload_model_root = runtime.start_container(run_dir)
        runtime.wait_for_home()
        initial_realism = dict(runtime.initialize_realism(payload_model_root))
        yield PX4RouteRuntimeSession(
            run_dir=run_dir,
            pose_trace_path=pose_trace_path,
            payload_model_root=payload_model_root,
            initial_realism=initial_realism,
        )
    finally:
        try:
            runtime.stop_container()
        finally:
            runtime.mark_cleanup_observed(run_dir)


__all__ = [
    "PX4RouteInitialRealism",
    "PX4RouteInitialRealismRuntime",
    "PX4RouteRuntimeLifecycle",
    "PX4RouteRuntimeSession",
    "collect_initial_realism",
    "px4_route_runtime_session",
]
