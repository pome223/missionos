#!/usr/bin/env python3
"""Opt-in real-hardware serial smoke for the PX4 read-only observation backend.

UNLIKE ``smoke_px4_real_hardware_readonly_observation.py`` (which uses an
in-process fixture reader and touches nothing), this script opens a **real USB
serial link** to a Pixhawk and ingests live telemetry through the read-only HIL
boundary. It is therefore the one script in this slice that requires real
hardware, and it is gated so it does nothing by default.

Safety gate
-----------

Nothing happens unless ``RUN_PX4_REAL_HARDWARE_SERIAL_SMOKE=1`` is set. Without
it, this script prints a skip notice and exits 0 — no serial device is opened,
no pymavlink import is attempted. When the gate is on, the operator must ALSO
attest the physical bench state via the environment:

    RUN_PX4_REAL_HARDWARE_SERIAL_SMOKE=1   # arm the gate
    PX4_SERIAL_DEVICE=/dev/tty.usbmodem1234
    PX4_BAUDRATE=57600                      # optional (default 57600)
    PX4_ATTESTING_OPERATOR_ID=your-id
    PX4_PROPELLERS_REMOVED=1                # you confirm props are OFF
    PX4_OPERATOR_PRESENT=1                  # you are physically at the bench
    PX4_BENCH_PHOTO_REF=...                 # optional evidence pointer
    PX4_SUBJECT_ID=pixhawk-bench-real-001   # optional
    PX4_SAMPLE_COUNT=3                       # optional

The reader is still structurally read-only: the link is wrapped so it cannot
transmit, and the backend refuses every actuator verb. This smoke only observes.

Run (operator, at the bench)::

    RUN_PX4_REAL_HARDWARE_SERIAL_SMOKE=1 PX4_SERIAL_DEVICE=/dev/tty.usbmodem1234 \
        PX4_ATTESTING_OPERATOR_ID=op-001 PX4_PROPELLERS_REMOVED=1 \
        PX4_OPERATOR_PRESENT=1 PYTHONPATH=. .venv/bin/python \
        scripts/smoke_px4_real_hardware_serial_observation.py
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from tempfile import TemporaryDirectory

from src.runtime.hil_telemetry_contract import HilTelemetryEnvelope
from src.runtime.px4_real_hardware_mavlink_reader import (
    PX4RealHardwarePhysicalAttestation,
    run_px4_real_hardware_readonly_observation,
)
from src.runtime.task_store import TaskStore

GATE_ENV = "RUN_PX4_REAL_HARDWARE_SERIAL_SMOKE"
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
                "smoke": "px4_real_hardware_serial_observation",
                "ran": False,
                "reason": (
                    f"{GATE_ENV} is not set to 1; this smoke opens a REAL serial "
                    "link and is skipped by default. No hardware touched."
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
    sample_count = int(os.environ.get("PX4_SAMPLE_COUNT", "3"))
    subject_id = os.environ.get("PX4_SUBJECT_ID", "pixhawk-bench-real-001")

    # The physical-safety attestation must be affirmatively set by the operator.
    # We never default these to True: an unset value means "not attested" and we
    # refuse to open the link. The model's Literal[True] fields then make it
    # impossible to construct an attestation that claims an unsafe bench.
    if not (_bool_env("PX4_PROPELLERS_REMOVED") and _bool_env("PX4_OPERATOR_PRESENT")):
        raise SystemExit(
            "refusing to open a real serial link without explicit physical-safety "
            "attestation: set PX4_PROPELLERS_REMOVED=1 and PX4_OPERATOR_PRESENT=1 "
            "only when the propellers are physically removed and you are at the bench"
        )
    attestation = PX4RealHardwarePhysicalAttestation(
        propellers_removed=True,
        operator_physically_present=True,
        attesting_operator_id=operator_id,
        attested_at=datetime.now(timezone.utc),
        bench_photo_evidence_ref=os.environ.get("PX4_BENCH_PHOTO_REF") or None,
    )

    # Honest boundary for a REAL run: hardware is powered and observed, but no
    # physical execution is ever invoked and the observation stays read-only.
    operational_safety_boundary = {
        "target_kind": "px4_real_hardware_readonly",
        "execution_source": "real_serial_pymavlink",
        "real_hardware_powered_on": True,
        "physical_setup_attestation_claimed": True,
        "propellers_removed": True,
        "operator_present": True,
        "physical_execution_invoked": False,
        "read_only_observation": True,
    }

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_real_hardware_readonly_observation",
            title="PX4 real-hardware serial read-only observation smoke",
            status="running",
            artifacts={"operational_safety_boundary": operational_safety_boundary},
        )
        updated = run_px4_real_hardware_readonly_observation(
            store=store,
            task_id=task["task_id"],
            subject_id=subject_id,
            serial_device=serial_device,
            baudrate=baudrate,
            opt_in=True,
            attestation=attestation,
            sample_count=sample_count,
        )

    observations = updated["artifacts"]["px4_real_hardware_readonly_observations"]
    contract = updated["artifacts"]["px4_real_hardware_readonly_contract"]
    envelopes = [HilTelemetryEnvelope.model_validate(obs) for obs in observations]
    batteries = [
        env.measurements["battery_voltage_v"]
        for env in envelopes
        if "battery_voltage_v" in env.measurements
    ]
    summary = {
        "smoke": "px4_real_hardware_serial_observation",
        "ran": True,
        "task_id": task["task_id"],
        "task_status": updated["status"],
        "serial_device": serial_device,
        "baudrate": baudrate,
        "observation_count": len(observations),
        "first_battery_v": batteries[0] if batteries else None,
        "last_battery_v": batteries[-1] if batteries else None,
        "any_armed_observed": any(
            bool(env.measurements.get("armed")) for env in envelopes
        ),
        "operational_safety_boundary_retained": (
            updated["artifacts"]["operational_safety_boundary"]
            == operational_safety_boundary
        ),
        "contract_mode": contract["mode"],
        "supports_action_dispatch": contract["supports_action_dispatch"],
        "supports_physical_execution": contract["supports_physical_execution"],
        "operator_approval_required": contract["operator_approval_required"],
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))

    assert summary["task_status"] == "running"
    assert summary["observation_count"] == sample_count
    assert summary["operational_safety_boundary_retained"] is True
    assert summary["contract_mode"] == "telemetry_only"
    assert summary["supports_action_dispatch"] is False
    assert summary["supports_physical_execution"] is False
    assert summary["operator_approval_required"] is True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
