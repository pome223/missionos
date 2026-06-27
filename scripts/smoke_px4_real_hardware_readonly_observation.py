#!/usr/bin/env python3
"""Runtime smoke for the PX4 real-hardware read-only observation backend.

Exercises the real production boundary this change affects — ``TaskStore``
persistence + HIL read-only ingestion + the deep-merge attach helper — WITHOUT
any hardware, SITL, Gazebo, ROS, MAVLink socket, or Docker. The telemetry
source is a deterministic in-process fixture reader, matching the
injected-reader contract the backend uses on a real bench.

Boundary covered::

    TaskStore.create
      -> PX4RealHardwareReadOnlyBackend.observe()   (ingest_hil_telemetry_envelope)
      -> attach_px4_real_hardware_readonly_observation_to_task()  (deep-merge)
      -> read back persisted task artifacts

Run::

    PYTHONPATH=. .venv/bin/python \
        scripts/smoke_px4_real_hardware_readonly_observation.py
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.hil_telemetry_contract import HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION
from src.runtime.px4_real_hardware_readonly_target import (
    PX4_REAL_HARDWARE_READONLY_CONTRACT_ID,
    PX4_REAL_HARDWARE_READONLY_SUBJECT_KIND,
    PX4RealHardwareReadOnlyBackend,
    attach_px4_real_hardware_readonly_observation_to_task,
    build_px4_real_hardware_readonly_contract,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)

# Attached at task creation; the attach helper must preserve it through the
# deep-merge. It carries no propellers_removed / operator_present attestation:
# this is a fixture run, not a real bench, and claiming otherwise would be a
# physical-attestation lie.
OPERATIONAL_SAFETY_BOUNDARY = {
    "target_kind": "px4_real_hardware_readonly",
    "execution_source": "fixture_reader",
    "real_hardware_powered_on": False,
    "physical_setup_attestation_claimed": False,
    "physical_execution_invoked": False,
    "read_only_observation": True,
}


def _make_fixture_reader():
    """Deterministic battery sag across the window; no command-like keys."""

    readings = [
        {"battery_voltage_v": 12.4, "armed": False, "gps_fix": "no_fix"},
        {"battery_voltage_v": 12.2, "armed": False, "gps_fix": "fix_3d"},
        {"battery_voltage_v": 12.1, "armed": False, "gps_fix": "fix_3d"},
    ]
    cursor = {"i": 0}

    def _read():
        measurements = readings[min(cursor["i"], len(readings) - 1)]
        cursor["i"] += 1
        return {
            "schema_version": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
            "contract_id": PX4_REAL_HARDWARE_READONLY_CONTRACT_ID,
            "subject_kind": PX4_REAL_HARDWARE_READONLY_SUBJECT_KIND,
            "subject_id": "pixhawk-bench-smoke-001",
            "captured_at": NOW.isoformat(),
            "measurements": dict(measurements),
            "metadata": {"link": "fixture_reader"},
        }

    return _read


def main() -> int:
    contract = build_px4_real_hardware_readonly_contract()
    backend = PX4RealHardwareReadOnlyBackend(
        telemetry_reader=_make_fixture_reader(),
        contract=contract,
    )

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_real_hardware_readonly_observation",
            title="PX4 real-hardware read-only observation smoke",
            status="running",
            artifacts={"operational_safety_boundary": OPERATIONAL_SAFETY_BOUNDARY},
        )

        observations = [backend.observe() for _ in range(3)]
        updated = attach_px4_real_hardware_readonly_observation_to_task(
            store=store,
            task_id=task["task_id"],
            contract=contract,
            observations=observations,
        )

    assert updated is not None
    persisted_obs = updated["artifacts"]["px4_real_hardware_readonly_observations"]
    persisted_contract = updated["artifacts"]["px4_real_hardware_readonly_contract"]
    summary = {
        "task_id": task["task_id"],
        "task_status": updated["status"],
        "operational_safety_boundary_retained": (
            updated["artifacts"]["operational_safety_boundary"]
            == OPERATIONAL_SAFETY_BOUNDARY
        ),
        "observation_count": len(persisted_obs),
        "first_battery_v": persisted_obs[0]["measurements"]["battery_voltage_v"],
        "last_battery_v": persisted_obs[-1]["measurements"]["battery_voltage_v"],
        "any_armed_observed": any(o["measurements"]["armed"] for o in persisted_obs),
        "envelope_schema": persisted_obs[0]["schema_version"],
        "contract_id": persisted_contract["contract_id"],
        "contract_mode": persisted_contract["mode"],
        "supports_action_dispatch": persisted_contract["supports_action_dispatch"],
        "supports_physical_execution": persisted_contract["supports_physical_execution"],
        "operator_approval_required": persisted_contract["operator_approval_required"],
        "requires_gcs_heartbeat": backend.requires_gcs_heartbeat,
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    assert summary["task_status"] == "running"
    assert summary["operational_safety_boundary_retained"] is True
    assert summary["observation_count"] == 3
    assert summary["first_battery_v"] == 12.4
    assert summary["last_battery_v"] == 12.1
    assert summary["any_armed_observed"] is False
    assert summary["envelope_schema"] == HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION
    assert summary["contract_id"] == PX4_REAL_HARDWARE_READONLY_CONTRACT_ID
    assert summary["contract_mode"] == "telemetry_only"
    assert summary["supports_action_dispatch"] is False
    assert summary["supports_physical_execution"] is False
    assert summary["operator_approval_required"] is True
    assert summary["requires_gcs_heartbeat"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
