"""PX4 real-hardware read-only mission target backend.

This module adds a **new backend behind the existing ``PX4MissionTarget``
Protocol**. Mission OS Core does not change: it talks to this backend through
the same target-invariant interface it already uses for ``PX4GazeboBackend``
(see :mod:`src.runtime.px4_mission_target`). The environment difference — a
real USB-serial Pixhawk versus Gazebo SITL — is absorbed *here, in the
adapter*. It is not absorbed in Core, and it is not exposed to the Agent as a
separate, environment-specific tool. The Agent keeps the same backend-neutral
verbs; the only thing that changes with the environment is a *descriptor*
(data), not a branch and not a new tool.

What this backend is
--------------------

A read-only observation backend. It satisfies ``PX4MissionTarget`` so Core can
dispatch the same verbs uniformly, but every actuator verb
(``upload_mission`` / ``arm`` / ``set_mode`` / ``start_mission``) is **refused
at the boundary** with ``PX4MissionTargetError``. Read-only is enforced by the
backend being *structurally incapable* of actuation, not by a counter that
claims zero frames were sent and not by a flag that could be flipped.

Only ``observe()`` (and the optionally-wired ``collect_events()``) do work, and
what they return has passed through the existing HIL telemetry-only ingestion
(:func:`ingest_hil_telemetry_envelope`), which fails closed on any
command-like key anywhere in the payload.

Why reuse, not reinvent
-----------------------

- ``PX4MissionTarget`` already *is* the environment-invariant seam. A
  real-hardware backend is simply another implementation of it, so Core stays
  byte-for-byte unchanged.
- ``HilTelemetryContract`` / ``HilTelemetryEnvelope`` already describe a
  read-only, telemetry-only ingestion surface with action / command / live /
  physical / ROS dispatch capabilities pinned to ``False`` at the type level.
  Real-hardware telemetry rides that same path instead of a bespoke one.

What this backend is NOT
------------------------

- It does **not** open a serial device, import ``pymavlink``, or power
  anything on. The telemetry source is *injected* as a reader callable. In CI
  the reader is a deterministic fixture; on a real bench (operator present,
  propellers removed) the reader is a passive ``pymavlink`` listener. The
  backend itself is environment-neutral and never fabricates a real-hardware
  attestation.
- It does **not** grant any control authority. The actuator verbs are not
  "present but ``False``" — they raise. There is no flag that turns them on.
  A future arm / mode-change / mission-upload backend is a *separate* backend
  with its own approval story, not a relaxation of this one.
- It does **not** change Core. No Core module branches on backend identity.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryContract,
    HilTelemetryEnvelope,
    HilTelemetryMode,
    ingest_hil_telemetry_envelope,
)
from src.runtime.px4_mission_target import (
    AbortReceipt,
    MissionEvent,
    PX4MissionTargetError,
    TargetTelemetry,
)


PX4_REAL_HARDWARE_READONLY_CONTRACT_ID = "px4_real_hardware_readonly.v1"
PX4_REAL_HARDWARE_READONLY_SUBJECT_KIND = "px4_real_hardware"


TelemetryReading = Mapping[str, Any] | HilTelemetryEnvelope
TelemetryReader = Callable[[], TelemetryReading]
TelemetryWindowReader = Callable[[], Sequence[TelemetryReading]]


def build_px4_real_hardware_readonly_contract(
    *,
    contract_id: str = PX4_REAL_HARDWARE_READONLY_CONTRACT_ID,
    subject_kind: str = PX4_REAL_HARDWARE_READONLY_SUBJECT_KIND,
) -> HilTelemetryContract:
    """Return the HIL telemetry-only descriptor this backend publishes.

    This is the *descriptor* the environment difference flows through. It is
    data, not a code branch: a real-hardware read-only path advertises itself
    as ``telemetry_only`` with every action / command / live / physical / ROS
    dispatch capability pinned to ``False`` at the type level via
    ``hil_telemetry_contract.v1``. Any path that needs even a bounded write
    must publish a different contract version with its own approval story.
    """

    return HilTelemetryContract(
        contract_id=contract_id,
        subject_kind=subject_kind,
        telemetry_envelope_schema=HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        mode=HilTelemetryMode.TELEMETRY_ONLY,
    )


@dataclass(frozen=True)
class PX4RealHardwareReadOnlyBackend:
    """Read-only PX4 mission target backed by real-hardware-shaped telemetry.

    Implements ``PX4MissionTarget``. ``observe()`` returns the latest
    HIL-ingested telemetry envelope from the injected ``telemetry_reader``;
    ``collect_events()`` drains the optional ``telemetry_window_reader`` (and
    returns ``[]`` when none is wired). Every actuator verb raises
    ``PX4MissionTargetError``: read-only is enforced at the boundary, not by a
    counter and not by a flag.

    The backend takes **no** actuator handlers. Unlike ``PX4GazeboBackend`` it
    cannot be handed an ``arm_handler`` / ``set_mode_handler`` /
    ``upload_mission_handler`` / ``start_mission_handler`` even by accident:
    those parameters do not exist on this type. That absence is itself a
    type-level guarantee that this backend cannot actuate.
    """

    telemetry_reader: TelemetryReader
    contract: HilTelemetryContract = field(
        default_factory=build_px4_real_hardware_readonly_contract
    )
    telemetry_window_reader: TelemetryWindowReader | None = None
    # A passive read-only listener emits nothing on the wire, so it does not
    # participate in the GCS heartbeat handshake.
    requires_gcs_heartbeat: bool = False

    # -- actuator verbs: refused at the boundary -------------------------

    def upload_mission(self, mission_plan: Any) -> Any:
        raise PX4MissionTargetError(
            "PX4RealHardwareReadOnlyBackend refuses upload_mission: "
            "read-only backend has no actuation path"
        )

    def arm(self) -> Any:
        raise PX4MissionTargetError(
            "PX4RealHardwareReadOnlyBackend refuses arm: "
            "read-only backend has no actuation path"
        )

    def set_mode(self, mode: str) -> Any:
        raise PX4MissionTargetError(
            "PX4RealHardwareReadOnlyBackend refuses set_mode: "
            "read-only backend has no actuation path"
        )

    def start_mission(self) -> Any:
        raise PX4MissionTargetError(
            "PX4RealHardwareReadOnlyBackend refuses start_mission: "
            "read-only backend has no actuation path"
        )

    # -- observation verbs: the only ones that do work ------------------

    def observe(self) -> TargetTelemetry:
        """Return the latest telemetry as a validated ``HilTelemetryEnvelope``.

        The injected reader provides a raw reading; this method runs it through
        ``ingest_hil_telemetry_envelope`` so any command-like key fails closed
        before the envelope is returned, then binds it to this backend's
        published contract.
        """

        return self._ingest(self.telemetry_reader())

    def collect_events(self) -> list[MissionEvent]:
        """Drain a window of telemetry envelopes, or ``[]`` if none is wired.

        Like ``PX4GazeboBackend.collect_events``, this returns an empty list
        when no window reader is configured. When wired, each reading in the
        window is ingested through the same read-only HIL boundary as
        ``observe()``.
        """

        if self.telemetry_window_reader is None:
            return []
        return [self._ingest(reading) for reading in self.telemetry_window_reader()]

    def stop_or_abort(self) -> AbortReceipt:
        """No-op abort: a read-only backend has nothing to stop and must not
        emit a frame to do so. An abort call must never itself fail.
        """

        return {
            "abort_attempted": False,
            "reason": "read_only_backend_has_no_actuation_to_abort",
        }

    # -- internal -------------------------------------------------------

    def _ingest(self, reading: TelemetryReading) -> HilTelemetryEnvelope:
        envelope = ingest_hil_telemetry_envelope(reading)
        if envelope.contract_id != self.contract.contract_id:
            raise PX4MissionTargetError(
                "PX4RealHardwareReadOnlyBackend refuses telemetry bound to a "
                f"foreign contract: envelope.contract_id={envelope.contract_id!r} "
                f"!= backend contract_id={self.contract.contract_id!r}"
            )
        return envelope


def attach_px4_real_hardware_readonly_observation_to_task(
    *,
    store: Any,
    task_id: str,
    contract: HilTelemetryContract,
    observations: Sequence[HilTelemetryEnvelope],
) -> dict[str, Any]:
    """Persist the published contract and observed telemetry onto a task.

    Mirrors the ``mock_hil_telemetry_source`` attach pattern: the artifacts
    are built first, then written through ``TaskStore.update(task_id,
    artifacts=...)`` whose deep-merge preserves any pre-existing artifact (for
    example an ``operational_safety_boundary`` attached at task creation). The
    helper does not change ``task.status`` / approvals / promotion state — it
    only writes the two read-only observation artifact keys.
    """

    artifacts = {
        "px4_real_hardware_readonly_contract": contract.model_dump(mode="json"),
        "px4_real_hardware_readonly_observations": [
            envelope.model_dump(mode="json") for envelope in observations
        ],
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise PX4MissionTargetError(
            f"task not found while attaching read-only observation: {task_id}"
        )
    return updated


__all__ = [
    "PX4_REAL_HARDWARE_READONLY_CONTRACT_ID",
    "PX4_REAL_HARDWARE_READONLY_SUBJECT_KIND",
    "PX4RealHardwareReadOnlyBackend",
    "TelemetryReader",
    "TelemetryReading",
    "TelemetryWindowReader",
    "attach_px4_real_hardware_readonly_observation_to_task",
    "build_px4_real_hardware_readonly_contract",
]
