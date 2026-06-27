#!/usr/bin/env python3
"""Opt-in PX4 real-hardware actuator bench smoke: arm -> disarm.

This script opens a REAL serial link and sends real MAVLink actuator commands
when, and only when, the operator explicitly arms the environment gate. It is
skipped by default and touches no hardware unless all gate variables are set.

Bench scope only:

- propellers must be removed
- operator must be physically present
- flight/takeoff authority remains false
- the command path is arm then disarm, with COMMAND_ACK and state readback

Run at the bench only::

    RUN_PX4_REAL_HARDWARE_ACTUATOR_SMOKE=1 \
      PX4_SERIAL_DEVICE=/dev/tty.usbmodem1234 \
      PX4_ATTESTING_OPERATOR_ID=operator-001 \
      PX4_PROPELLERS_REMOVED=1 PX4_OPERATOR_PRESENT=1 \
      PYTHONPATH=. .venv/bin/python \
      scripts/smoke_px4_real_hardware_actuator_arm_disarm.py
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from tempfile import TemporaryDirectory

from src.runtime.px4_real_hardware_actuator_backend import (
    build_px4_real_hardware_actuator_approval,
    run_px4_real_hardware_arm_disarm_bench,
)
from src.runtime.px4_real_hardware_mavlink_reader import (
    PX4RealHardwarePhysicalAttestation,
)
from src.runtime.task_store import TaskStore

GATE_ENV = "RUN_PX4_REAL_HARDWARE_ACTUATOR_SMOKE"
_TRUE = {"1", "true", "yes", "on"}


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{GATE_ENV} is armed but required env var {name} is unset; refusing to run"
        )
    return value


def _skip() -> int:
    print(
        json.dumps(
            {
                "smoke": "px4_real_hardware_actuator_arm_disarm",
                "ran": False,
                "reason": (
                    f"{GATE_ENV} is not set to 1; this smoke opens a REAL serial "
                    "link and sends arm/disarm MAVLink commands. No hardware touched."
                ),
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    if not _bool_env(GATE_ENV):
        return _skip()

    serial_device = _require("PX4_SERIAL_DEVICE")
    operator_id = _require("PX4_ATTESTING_OPERATOR_ID")
    baudrate = int(os.environ.get("PX4_BAUDRATE", "57600"))
    subject_id = os.environ.get("PX4_SUBJECT_ID", "pixhawk-bench-real-001")

    if not (_bool_env("PX4_PROPELLERS_REMOVED") and _bool_env("PX4_OPERATOR_PRESENT")):
        raise SystemExit(
            "refusing actuator smoke without explicit physical-safety attestation: "
            "set PX4_PROPELLERS_REMOVED=1 and PX4_OPERATOR_PRESENT=1 only when "
            "propellers are physically removed and you are at the bench"
        )

    now = datetime.now(timezone.utc)
    attestation = PX4RealHardwarePhysicalAttestation(
        propellers_removed=True,
        operator_physically_present=True,
        attesting_operator_id=operator_id,
        attested_at=now,
        bench_photo_evidence_ref=os.environ.get("PX4_BENCH_PHOTO_REF") or None,
    )
    approval = build_px4_real_hardware_actuator_approval(
        approved_operations=("arm", "disarm"),
        physical_attestation=attestation,
        now=now,
        metadata={"smoke": "px4_real_hardware_actuator_arm_disarm"},
    )

    operational_safety_boundary = {
        "target_kind": "px4_real_hardware_actuator",
        "execution_source": "real_serial_pymavlink",
        "real_hardware_powered_on": True,
        "physical_setup_attestation_claimed": True,
        "propellers_removed": True,
        "operator_present": True,
        "physical_execution_invoked": True,
        "flight_execution_invoked": False,
        "takeoff_invoked": False,
    }

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_real_hardware_actuator_arm_disarm",
            title="PX4 real-hardware actuator arm/disarm bench smoke",
            status="running",
            artifacts={"operational_safety_boundary": operational_safety_boundary},
        )
        result = run_px4_real_hardware_arm_disarm_bench(
            store=store,
            task_id=task["task_id"],
            subject_id=subject_id,
            approval=approval,
            serial_device=serial_device,
            baudrate=baudrate,
            opt_in=True,
        )
        updated = store.get(task["task_id"])

    assert updated is not None
    invocations = updated["artifacts"]["px4_real_hardware_actuator_invocations"]
    arm_evidence = result["arm"]["command_evidence"]
    summary = {
        "smoke": "px4_real_hardware_actuator_arm_disarm",
        "ran": True,
        "task_id": task["task_id"],
        "serial_device": serial_device,
        "baudrate": baudrate,
        "arm_status": result["arm"]["status"],
        "disarm_status": result["disarm"]["status"],
        "invocation_count": len(invocations),
        "command_evidence_schema": arm_evidence["schema_version"],
        "arm_ack_observed": result["arm"]["command_ack_observed"],
        "arm_state_readback_observed": result["arm"]["state_readback_observed"],
        "disarm_ack_observed": result["disarm"]["command_ack_observed"],
        "disarm_state_readback_observed": result["disarm"]["state_readback_observed"],
        "link_kind": result["arm"]["link_kind"],
        "physical_execution_invoked": result["arm"]["physical_execution_invoked"],
        "flight_execution_invoked": arm_evidence["flight_execution_invoked"],
        "takeoff_invoked": False,
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    assert summary["arm_status"] == "accepted"
    assert summary["disarm_status"] == "accepted"
    assert summary["invocation_count"] == 2
    assert summary["arm_ack_observed"] is True
    assert summary["arm_state_readback_observed"] is True
    assert summary["disarm_ack_observed"] is True
    assert summary["disarm_state_readback_observed"] is True
    # This smoke opens the real serial link via the gated opener, so the
    # persisted evidence must carry real provenance, not the fake default.
    assert summary["link_kind"] == "real_serial_pymavlink"
    assert summary["physical_execution_invoked"] is True
    assert summary["flight_execution_invoked"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
