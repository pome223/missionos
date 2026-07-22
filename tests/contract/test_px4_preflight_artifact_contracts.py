"""Contracts replacing logic-only PX4 profile/observation/preflight smokes.

These tests exercise TaskStore persistence and the false-authority fields that
the former standalone smoke programs asserted.  They do not start SITL,
Gazebo, a network transport, or hardware.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.runtime.px4_delivery_command_preflight import (
    attach_px4_simulation_command_preflight_artifacts,
)
from src.runtime.px4_gazebo_delivery_world_profile import (
    PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION,
    attach_px4_gazebo_delivery_world_profile,
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_sitl_delivery_observation import (
    PX4_SITL_DELIVERY_OBSERVATION_SCHEMA_VERSION,
    attach_px4_sitl_delivery_observation,
    build_px4_sitl_delivery_observation_from_logs,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PX4_SITL_LOGS = "\n".join(
    (
        "INFO  [px4] startup script: /bin/sh etc/init.d-posix/rcS 0",
        "INFO  [init] found model autostart file as SYS_AUTOSTART=10040",
        "INFO  [init] SIH simulator",
        "INFO  [simulator_sih] Simulation loop with 250 Hz",
        "INFO  [logger] logger started (mode=all)",
        "INFO  [px4] Startup script returned successfully",
    )
)
FALSE_AUTHORITY_FIELDS = (
    "command_payload_allowed",
    "dispatch_implementation_present",
    "ros_dispatch_allowed",
    "mavlink_dispatch_allowed",
    "hardware_target_allowed",
    "physical_execution_invoked",
)


def _store_with_task(tmp_path: Path, kind: str) -> tuple[TaskStore, dict[str, Any]]:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind=kind,
        title=f"{kind} contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    return store, task


def _assert_false_authority(record: Mapping[str, Any]) -> None:
    for field in FALSE_AUTHORITY_FIELDS:
        assert record[field] is False, field


def test_world_profile_attach_is_descriptor_only_and_preserves_task(
    tmp_path: Path,
) -> None:
    store, task = _store_with_task(tmp_path, "px4_gazebo_delivery_world_profile")

    attached = attach_px4_gazebo_delivery_world_profile(
        task["task_id"],
        now=NOW,
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])
    profile = attached["px4_gazebo_delivery_world_profile"]

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert profile["schema_version"] == PX4_GAZEBO_DELIVERY_WORLD_PROFILE_SCHEMA_VERSION
    assert profile["simulation_only"] is True
    assert profile["telemetry_first"] is True
    assert profile["profile_descriptor_only"] is True
    assert profile["operator_approval_required_for_commands"] is True
    assert profile["command_surface_present"] is False
    assert profile["live_execution_allowed"] is False
    assert profile["px4_mission_upload_allowed"] is False
    assert profile["gazebo_entity_mutation_allowed"] is False
    assert profile["actuator_execution_allowed"] is False
    assert profile["network_ports_exposed"] is False
    _assert_false_authority(profile)


def test_sitl_observation_attach_is_read_only_and_preserves_task(
    tmp_path: Path,
) -> None:
    store, task = _store_with_task(tmp_path, "px4_sitl_delivery_observation")
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)

    attached = attach_px4_sitl_delivery_observation(
        task["task_id"],
        log_text=PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])
    observation = attached["px4_sitl_delivery_observation"]

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert observation["schema_version"] == PX4_SITL_DELIVERY_OBSERVATION_SCHEMA_VERSION
    assert observation["measurements"]["px4_sitl_started"] is True
    assert observation["simulation_only"] is True
    assert observation["telemetry_only"] is True
    assert observation["read_only"] is True
    assert observation["command_surface_present"] is False
    assert observation["live_execution_allowed"] is False
    assert observation["px4_mission_upload_allowed"] is False
    assert observation["gazebo_entity_mutation_allowed"] is False
    assert observation["actuator_execution_allowed"] is False
    _assert_false_authority(observation)


def test_command_preflight_attach_keeps_approval_separate_from_dispatch(
    tmp_path: Path,
) -> None:
    store, task = _store_with_task(tmp_path, "px4_simulation_command_preflight")
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    observation = build_px4_sitl_delivery_observation_from_logs(
        PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
    )

    attached = attach_px4_simulation_command_preflight_artifacts(
        task["task_id"],
        profile=profile,
        observation=observation,
        operator_approval_performed=True,
        now=NOW,
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    connection = attached["px4_simulation_mavlink_connection_contract"]
    adapter = attached["px4_simulation_mavlink_telemetry_adapter"]
    proposal = attached["px4_simulation_delivery_command_proposal"]
    approval = attached["px4_simulation_command_approval"]
    allowlist = attached["px4_simulation_command_allowlist"]
    assert connection["connection_opened"] is False
    assert connection["telemetry_observation_only"] is True
    assert adapter["adapter_mode"] == "telemetry_observation_only"
    assert adapter["command_frames_observed"] == 0
    assert approval["operator_approval_performed"] is True
    assert allowlist["raw_command_payload_allowed"] is False
    for artifact in (connection, adapter, proposal, approval, allowlist):
        _assert_false_authority(artifact)
