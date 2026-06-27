"""Approval-gated PX4 real-hardware actuator backend.

This is the first real-hardware backend in this branch that can execute a
bounded MAVLink actuator command. It is deliberately separate from
``PX4RealHardwareReadOnlyBackend``: read-only observation stays read-only, while
this backend requires operator approval before any send path is reachable.

The core invariant is executable, not documentary:

    approval check -> MAVLink send -> COMMAND_ACK -> state readback -> TaskStore

The module is duck-typed around a pymavlink-like connection so fake integration
tests can exercise the whole path without hardware or ``pymavlink`` installed.
The real bench smoke supplies the real connection only after an explicit env
gate and physical attestation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.hil_telemetry_contract import (
    HilTelemetryContract,
    HilTelemetryEnvelope,
    ingest_hil_telemetry_envelope,
)
from src.runtime.px4_mavlink_ack_state import (
    MAV_RESULT_ACCEPTED,
)
from src.runtime.px4_mission_target import PX4MissionTargetError
from src.runtime.px4_real_hardware_mavlink_reader import (
    DEFAULT_ATTESTATION_STALENESS_SECONDS,
    PX4RealHardwarePhysicalAttestation,
    SUPPORTED_BAUDRATES,
    mavlink_message_to_reading,
)
from src.runtime.px4_real_hardware_readonly_target import (
    attach_px4_real_hardware_readonly_observation_to_task,
    build_px4_real_hardware_readonly_contract,
)


PX4_REAL_HARDWARE_ACTUATOR_APPROVAL_SCHEMA_VERSION = (
    "px4_real_hardware_actuator_approval.v1"
)
PX4_REAL_HARDWARE_ACTUATOR_INVOCATION_SCHEMA_VERSION = (
    "px4_real_hardware_actuator_invocation.v1"
)
# This backend is a downstream executor callee, not the dispatch runtime. The
# canonical ``runtime_invocation_evidence.v1`` (validated by
# ``src/runtime/runtime_claim_evidence.py``) is owned by the dispatch runtime and
# carries invocation_kind/target/timestamps/sha256/exit_code. The record below is
# a command-level readback record only, so it gets its own distinct schema name
# and must NOT claim the canonical name.
PX4_REAL_HARDWARE_ACTUATOR_COMMAND_EVIDENCE_SCHEMA_VERSION = (
    "px4_real_hardware_actuator_command_evidence.v1"
)

# Provenance of the MAVLink link an invocation ran over. ``real_serial_pymavlink``
# is set *only* by ``open_px4_real_hardware_actuator_serial`` (the gated real
# opener) via :func:`mark_connection_real_serial`. Every other connection — every
# fake, every injected stand-in — resolves to ``injected_fake``. Unlabeled means
# not real, so a fake run can never present itself as a real-hardware run in the
# persisted evidence, and ``physical_execution_invoked`` is derived from this,
# never hardcoded.
LINK_KIND_INJECTED_FAKE = "injected_fake"
LINK_KIND_REAL_SERIAL_PYMAVLINK = "real_serial_pymavlink"
_CONNECTION_LINK_KIND_ATTR = "_px4_actuator_link_kind"

MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_DO_SET_MODE = 176
MAV_CMD_MISSION_START = 300

PX4_CUSTOM_MAIN_MODE_MANUAL = 1
PX4_CUSTOM_MAIN_MODE_ALTCTL = 2
PX4_CUSTOM_MAIN_MODE_POSCTL = 3
PX4_CUSTOM_MAIN_MODE_AUTO = 4
PX4_CUSTOM_SUB_MODE_AUTO_MISSION = 4

_COMMAND_NAMES = {
    MAV_CMD_COMPONENT_ARM_DISARM: "MAV_CMD_COMPONENT_ARM_DISARM",
    MAV_CMD_DO_SET_MODE: "MAV_CMD_DO_SET_MODE",
    MAV_CMD_MISSION_START: "MAV_CMD_MISSION_START",
}

_MODE_PARAMS = {
    "MANUAL": (1.0, float(PX4_CUSTOM_MAIN_MODE_MANUAL), 0.0),
    "ALTCTL": (1.0, float(PX4_CUSTOM_MAIN_MODE_ALTCTL), 0.0),
    "POSCTL": (1.0, float(PX4_CUSTOM_MAIN_MODE_POSCTL), 0.0),
    "AUTO.MISSION": (
        1.0,
        float(PX4_CUSTOM_MAIN_MODE_AUTO),
        float(PX4_CUSTOM_SUB_MODE_AUTO_MISSION),
    ),
}

_SERIAL_DEVICE_PATTERN = re.compile(r"^/dev/(tty|cu)[A-Za-z0-9._-]*$")


class PX4RealHardwareActuatorError(PX4MissionTargetError):
    """Raised when a real-hardware actuator path cannot proceed safely."""


class PX4RealHardwareActuatorOperation(str, Enum):
    ARM = "arm"
    DISARM = "disarm"
    SET_MODE = "set_mode"
    START_MISSION = "start_mission"
    UPLOAD_MISSION = "upload_mission"


class PX4RealHardwareActuatorStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


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


def _ordered_operations(values: Sequence[str | PX4RealHardwareActuatorOperation]) -> tuple[
    PX4RealHardwareActuatorOperation, ...
]:
    seen: set[PX4RealHardwareActuatorOperation] = set()
    out: list[PX4RealHardwareActuatorOperation] = []
    for value in values:
        operation = (
            value
            if isinstance(value, PX4RealHardwareActuatorOperation)
            else PX4RealHardwareActuatorOperation(str(value))
        )
        if operation not in seen:
            seen.add(operation)
            out.append(operation)
    return tuple(out)


def _approval_ref(approval: "PX4RealHardwareActuatorApproval") -> str:
    return f"px4_real_hardware_actuator_approval:{approval.approval_id}"


def _command_name(command_id: int) -> str:
    try:
        return _COMMAND_NAMES[int(command_id)]
    except KeyError as exc:
        raise PX4RealHardwareActuatorError(
            f"unsupported real-hardware actuator command id: {command_id}"
        ) from exc


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def mark_connection_real_serial(connection: Any) -> Any:
    """Tag a connection as the real serial pymavlink link, then return it.

    Called once by :func:`open_px4_real_hardware_actuator_serial` on the live
    connection it just opened. That is the *only* production caller, so the
    ``real_serial_pymavlink`` provenance is bound to an actual gated real-open
    event — it cannot be asserted by a backend constructor flag a CI fixture
    could flip.
    """

    try:
        setattr(connection, _CONNECTION_LINK_KIND_ATTR, LINK_KIND_REAL_SERIAL_PYMAVLINK)
    except (AttributeError, TypeError):  # pragma: no cover - mavutil allows attrs
        pass
    return connection


def _connection_link_kind(connection: Any) -> str:
    if (
        getattr(connection, _CONNECTION_LINK_KIND_ATTR, None)
        == LINK_KIND_REAL_SERIAL_PYMAVLINK
    ):
        return LINK_KIND_REAL_SERIAL_PYMAVLINK
    return LINK_KIND_INJECTED_FAKE


class PX4RealHardwareActuatorApproval(BaseModel):
    """Operator approval that is required before any actuator send.

    This approval is intentionally bench-scoped for the first actuator slice:
    propellers must be removed and flight/takeoff authority is false. It allows
    real MAVLink actuator calls such as arm/disarm to be tested at the bench,
    but does not authorize takeoff or autonomous flight.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_REAL_HARDWARE_ACTUATOR_APPROVAL_SCHEMA_VERSION] = (
        PX4_REAL_HARDWARE_ACTUATOR_APPROVAL_SCHEMA_VERSION
    )
    approval_id: str
    operator_approval_performed: Literal[True]
    approved_operations: tuple[PX4RealHardwareActuatorOperation, ...]
    physical_attestation: PX4RealHardwarePhysicalAttestation
    approved_at: datetime
    physical_actuator_execution_allowed: Literal[True] = True
    flight_execution_allowed: Literal[False] = False
    takeoff_allowed: Literal[False] = False
    mission_start_allowed: bool = False
    operator_approval_required: Literal[True] = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("approved_operations", mode="before")
    @classmethod
    def _coerce_operations(cls, value: Any) -> tuple[PX4RealHardwareActuatorOperation, ...]:
        return _ordered_operations(value or ())

    @field_validator("approved_at", mode="before")
    @classmethod
    def _coerce_approved_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_approval(self) -> "PX4RealHardwareActuatorApproval":
        if not self.approved_operations:
            raise PX4RealHardwareActuatorError(
                "real-hardware actuator approval requires approved operations"
            )
        if (
            PX4RealHardwareActuatorOperation.START_MISSION in self.approved_operations
            and self.mission_start_allowed is not True
        ):
            raise PX4RealHardwareActuatorError(
                "start_mission approval requires mission_start_allowed=true"
            )
        self.physical_attestation.ensure_fresh(now=self.approved_at)
        return self


class PX4RealHardwareActuatorInvocation(BaseModel):
    """Evidence for one actuator command invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_REAL_HARDWARE_ACTUATOR_INVOCATION_SCHEMA_VERSION] = (
        PX4_REAL_HARDWARE_ACTUATOR_INVOCATION_SCHEMA_VERSION
    )
    invocation_id: str
    operation: PX4RealHardwareActuatorOperation
    approval_ref: str
    link_kind: str
    physical_execution_invoked: bool
    command_evidence: dict[str, Any]
    command_id: int
    command_name: str
    command_params: tuple[float, float, float, float, float, float, float]
    mavlink_command_sent: bool
    command_ack_wait_performed: Literal[True] = True
    command_ack_observed: bool
    command_ack_result_code: int | None = None
    command_ack_result_name: str | None = None
    state_readback_performed: Literal[True] = True
    state_readback_observed: bool
    state_readback: dict[str, Any] | None = None
    status: PX4RealHardwareActuatorStatus
    blocked_reasons: tuple[str, ...] = ()
    observed_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_observed_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_invocation(self) -> "PX4RealHardwareActuatorInvocation":
        if self.command_name != _command_name(self.command_id):
            raise PX4RealHardwareActuatorError("command name does not match command id")
        if self.physical_execution_invoked != (
            self.link_kind == LINK_KIND_REAL_SERIAL_PYMAVLINK
        ):
            raise PX4RealHardwareActuatorError(
                "physical_execution_invoked must be true iff link_kind is "
                "real_serial_pymavlink"
            )
        runtime = self.command_evidence
        required_runtime = {
            "schema_version": PX4_REAL_HARDWARE_ACTUATOR_COMMAND_EVIDENCE_SCHEMA_VERSION,
            "boundary": "real_hardware_mavlink",
            "link_kind": self.link_kind,
            "physical_execution_invoked": self.physical_execution_invoked,
            "mavlink_command_sent": self.mavlink_command_sent,
            "command_ack_observed": self.command_ack_observed,
            "state_readback_observed": self.state_readback_observed,
        }
        for key, expected in required_runtime.items():
            if runtime.get(key) != expected:
                raise PX4RealHardwareActuatorError(
                    f"command_evidence.{key} mismatch"
                )
        if self.status == PX4RealHardwareActuatorStatus.ACCEPTED:
            if not (
                self.mavlink_command_sent
                and self.command_ack_observed
                and self.command_ack_result_code == MAV_RESULT_ACCEPTED
                and self.state_readback_observed
                and self.state_readback is not None
            ):
                raise PX4RealHardwareActuatorError(
                    "accepted actuator invocation requires send, accepted ACK, "
                    "and state readback"
                )
            if self.blocked_reasons:
                raise PX4RealHardwareActuatorError(
                    "accepted actuator invocation cannot include blocked reasons"
                )
        elif not self.blocked_reasons:
            raise PX4RealHardwareActuatorError(
                "non-accepted actuator invocation requires blocked reasons"
            )
        return self


@dataclass
class PX4RealHardwareActuatorBackend:
    """PX4MissionTarget backend with approval-gated actuator sends."""

    connection: Any
    approval: PX4RealHardwareActuatorApproval
    subject_id: str
    contract: HilTelemetryContract = field(
        default_factory=build_px4_real_hardware_readonly_contract
    )
    store: Any | None = None
    task_id: str | None = None
    clock: Any = field(default=lambda: datetime.now(timezone.utc))
    ack_timeout_seconds: float = 3.0
    state_timeout_seconds: float = 3.0
    requires_gcs_heartbeat: bool = False

    def upload_mission(self, mission_plan: Any) -> dict[str, Any]:
        self._require_operation(PX4RealHardwareActuatorOperation.UPLOAD_MISSION)
        uploader = getattr(self.connection, "upload_mission", None)
        if not callable(uploader):
            raise PX4RealHardwareActuatorError(
                "connection does not expose upload_mission; real mission upload "
                "requires a dedicated uploader implementation"
            )
        result = uploader(mission_plan)
        self._persist_extra(
            "px4_real_hardware_mission_upload_results",
            [dict(result) if isinstance(result, Mapping) else {"result": result}],
        )
        return dict(result) if isinstance(result, Mapping) else {"result": result}

    def arm(self) -> dict[str, Any]:
        return self._run_command(
            operation=PX4RealHardwareActuatorOperation.ARM,
            command_id=MAV_CMD_COMPONENT_ARM_DISARM,
            params=(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            expected_state={"armed": True},
        )

    def disarm(self) -> dict[str, Any]:
        return self._run_command(
            operation=PX4RealHardwareActuatorOperation.DISARM,
            command_id=MAV_CMD_COMPONENT_ARM_DISARM,
            params=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            expected_state={"armed": False},
        )

    def set_mode(self, mode: str) -> dict[str, Any]:
        normalized = mode.strip().upper()
        if normalized not in _MODE_PARAMS:
            raise PX4RealHardwareActuatorError(
                f"unsupported real-hardware mode {mode!r}; expected one of "
                f"{sorted(_MODE_PARAMS)}"
            )
        base_mode, custom_mode, custom_sub_mode = _MODE_PARAMS[normalized]
        return self._run_command(
            operation=PX4RealHardwareActuatorOperation.SET_MODE,
            command_id=MAV_CMD_DO_SET_MODE,
            params=(base_mode, custom_mode, custom_sub_mode, 0.0, 0.0, 0.0, 0.0),
            expected_state={"mode": normalized},
        )

    def start_mission(self) -> dict[str, Any]:
        return self._run_command(
            operation=PX4RealHardwareActuatorOperation.START_MISSION,
            command_id=MAV_CMD_MISSION_START,
            params=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            expected_state={"mission_started": True},
        )

    def observe(self) -> HilTelemetryEnvelope:
        msg = self._recv_state_message(timeout=self.state_timeout_seconds)
        envelope = self._message_to_envelope(msg)
        self._persist_observations([envelope])
        return envelope

    def collect_events(self) -> list[Any]:
        return []

    def stop_or_abort(self) -> dict[str, Any]:
        if PX4RealHardwareActuatorOperation.DISARM in self.approval.approved_operations:
            return self.disarm()
        return {
            "abort_attempted": False,
            "reason": "disarm_not_in_operator_approval",
        }

    def _require_operation(self, operation: PX4RealHardwareActuatorOperation) -> None:
        if operation not in self.approval.approved_operations:
            raise PX4RealHardwareActuatorError(
                f"operator approval does not include {operation.value}"
            )
        self.approval.physical_attestation.ensure_fresh(now=self.clock())

    def _run_command(
        self,
        *,
        operation: PX4RealHardwareActuatorOperation,
        command_id: int,
        params: tuple[float, float, float, float, float, float, float],
        expected_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require_operation(operation)
        sent_at = _utc(self.clock())
        self._send_command_long(command_id=command_id, params=params)
        ack = self._wait_command_ack(command_id)
        state = self._wait_state_readback(expected_state)
        status = (
            PX4RealHardwareActuatorStatus.ACCEPTED
            if ack.get("result_code") == MAV_RESULT_ACCEPTED and state is not None
            else PX4RealHardwareActuatorStatus.REJECTED
        )
        blocked = () if status == PX4RealHardwareActuatorStatus.ACCEPTED else (
            "command_ack_or_state_readback_not_accepted",
        )
        invocation = self._build_invocation(
            operation=operation,
            command_id=command_id,
            params=params,
            ack=ack,
            state=state,
            status=status,
            blocked_reasons=blocked,
            observed_at=sent_at,
        )
        self._persist_invocation(invocation)
        return invocation.model_dump(mode="json")

    def _send_command_long(
        self,
        *,
        command_id: int,
        params: tuple[float, float, float, float, float, float, float],
    ) -> None:
        mav = getattr(self.connection, "mav", None)
        sender = getattr(mav, "command_long_send", None)
        if not callable(sender):
            raise PX4RealHardwareActuatorError(
                "connection.mav.command_long_send is required for actuator execution"
            )
        sender(1, 1, int(command_id), 0, *params)

    def _wait_command_ack(self, command_id: int) -> dict[str, Any]:
        msg = self.connection.recv_match(
            type="COMMAND_ACK",
            blocking=True,
            timeout=self.ack_timeout_seconds,
        )
        if msg is None:
            raise PX4RealHardwareActuatorError(
                f"COMMAND_ACK timeout for {_command_name(command_id)}"
            )
        observed_command = int(_field(msg, "command"))
        result_code = int(_field(msg, "result"))
        if observed_command != int(command_id):
            raise PX4RealHardwareActuatorError(
                f"COMMAND_ACK command mismatch: {observed_command} != {command_id}"
            )
        return {
            "command_id": observed_command,
            "result_code": result_code,
            "result_name": "ACCEPTED" if result_code == MAV_RESULT_ACCEPTED else "REJECTED",
        }

    def _wait_state_readback(self, expected_state: Mapping[str, Any]) -> dict[str, Any] | None:
        # Bounded attempts keep fake tests with fixed clocks from spinning
        # forever while real clocks still get several readback opportunities.
        for _ in range(8):
            msg = self._recv_state_message(timeout=self.state_timeout_seconds)
            envelope = self._message_to_envelope(msg)
            measurements = envelope.model_dump(mode="json")["measurements"]
            if all(measurements.get(key) == value for key, value in expected_state.items()):
                self._persist_observations([envelope])
                return envelope.model_dump(mode="json")
        return None

    def _recv_state_message(self, *, timeout: float) -> Any:
        msg = self.connection.recv_match(
            type=["HEARTBEAT", "SYS_STATUS", "BATTERY_STATUS", "GPS_RAW_INT"],
            blocking=True,
            timeout=timeout,
        )
        if msg is None:
            raise PX4RealHardwareActuatorError("state readback timeout")
        return msg

    def _message_to_envelope(self, msg: Any) -> HilTelemetryEnvelope:
        reading = mavlink_message_to_reading(
            msg,
            contract_id=self.contract.contract_id,
            subject_kind=self.contract.subject_kind,
            subject_id=self.subject_id,
            captured_at=_utc(self.clock()),
            link_label="real_hardware_actuator_readback",
        )
        if reading is None:
            raise PX4RealHardwareActuatorError("state readback message carried no telemetry")
        return ingest_hil_telemetry_envelope(reading)

    def _build_invocation(
        self,
        *,
        operation: PX4RealHardwareActuatorOperation,
        command_id: int,
        params: tuple[float, float, float, float, float, float, float],
        ack: Mapping[str, Any],
        state: Mapping[str, Any] | None,
        status: PX4RealHardwareActuatorStatus,
        blocked_reasons: tuple[str, ...],
        observed_at: datetime,
    ) -> PX4RealHardwareActuatorInvocation:
        payload = {
            "operation": operation.value,
            "approval_ref": _approval_ref(self.approval),
            "command_id": command_id,
            "ack": dict(ack),
            "state_readback": state,
            "observed_at": observed_at.isoformat(),
        }
        command_sent = True
        ack_observed = bool(ack)
        state_observed = state is not None
        link_kind = _connection_link_kind(self.connection)
        physical_execution_invoked = link_kind == LINK_KIND_REAL_SERIAL_PYMAVLINK
        command_evidence = {
            "schema_version": PX4_REAL_HARDWARE_ACTUATOR_COMMAND_EVIDENCE_SCHEMA_VERSION,
            "boundary": "real_hardware_mavlink",
            "link_kind": link_kind,
            "operation": operation.value,
            "approval_ref": _approval_ref(self.approval),
            "command_id": command_id,
            "command_name": _command_name(command_id),
            "mavlink_command_sent": command_sent,
            "command_ack_observed": ack_observed,
            "command_ack_result_code": ack.get("result_code"),
            "state_readback_observed": state_observed,
            "physical_execution_invoked": physical_execution_invoked,
            "flight_execution_invoked": False,
            "observed_at": observed_at.isoformat(),
        }
        return PX4RealHardwareActuatorInvocation(
            invocation_id=_stable_id("px4_real_hardware_actuator_invocation", payload),
            operation=operation,
            approval_ref=_approval_ref(self.approval),
            link_kind=link_kind,
            physical_execution_invoked=physical_execution_invoked,
            command_evidence=command_evidence,
            command_id=command_id,
            command_name=_command_name(command_id),
            command_params=params,
            mavlink_command_sent=command_sent,
            command_ack_observed=ack_observed,
            command_ack_result_code=ack.get("result_code"),
            command_ack_result_name=ack.get("result_name"),
            state_readback_observed=state_observed,
            state_readback=dict(state) if state is not None else None,
            status=status,
            blocked_reasons=blocked_reasons,
            observed_at=observed_at,
        )

    def _persist_invocation(self, invocation: PX4RealHardwareActuatorInvocation) -> None:
        self._persist_extra(
            "px4_real_hardware_actuator_invocations",
            [invocation.model_dump(mode="json")],
        )

    def _persist_observations(self, observations: Sequence[HilTelemetryEnvelope]) -> None:
        if self.store is None or self.task_id is None:
            return
        attach_px4_real_hardware_readonly_observation_to_task(
            store=self.store,
            task_id=self.task_id,
            contract=self.contract,
            observations=observations,
        )

    def _persist_extra(self, key: str, values: Sequence[Mapping[str, Any]]) -> None:
        if self.store is None or self.task_id is None:
            return
        current = self.store.get(self.task_id)
        if current is None:
            raise PX4RealHardwareActuatorError(
                f"task not found while attaching actuator evidence: {self.task_id}"
            )
        existing = list(current.get("artifacts", {}).get(key, []))
        self.store.update(self.task_id, artifacts={key: [*existing, *values]})


def build_px4_real_hardware_actuator_approval(
    *,
    approval_id: str = "approval-real-hardware-actuator-bench",
    approved_operations: Sequence[str | PX4RealHardwareActuatorOperation],
    physical_attestation: PX4RealHardwarePhysicalAttestation | Mapping[str, Any],
    now: datetime | None = None,
    mission_start_allowed: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> PX4RealHardwareActuatorApproval:
    """Build an approval object for a bounded bench actuator run."""

    attestation = (
        physical_attestation
        if isinstance(physical_attestation, PX4RealHardwarePhysicalAttestation)
        else PX4RealHardwarePhysicalAttestation.model_validate(dict(physical_attestation))
    )
    return PX4RealHardwareActuatorApproval(
        approval_id=approval_id,
        operator_approval_performed=True,
        approved_operations=tuple(approved_operations),
        physical_attestation=attestation,
        approved_at=_utc(now),
        mission_start_allowed=mission_start_allowed,
        metadata=dict(metadata or {}),
    )


def _validate_actuator_open_inputs(
    *,
    serial_device: str,
    baudrate: int,
    opt_in: bool,
    attestation: PX4RealHardwarePhysicalAttestation,
    now: datetime | None,
    max_attestation_age_seconds: int,
) -> None:
    if opt_in is not True:
        raise PX4RealHardwareActuatorError(
            "real-hardware actuator execution requires explicit opt_in=true"
        )
    if not _SERIAL_DEVICE_PATTERN.match(serial_device):
        raise PX4RealHardwareActuatorError(
            "real-hardware actuator execution is restricted to a local serial "
            f"device (/dev/tty* or /dev/cu*); refused {serial_device!r}"
        )
    if baudrate not in SUPPORTED_BAUDRATES:
        raise PX4RealHardwareActuatorError(
            f"unsupported baudrate {baudrate}; expected one of "
            f"{sorted(SUPPORTED_BAUDRATES)}"
        )
    attestation.ensure_fresh(now=now, max_age_seconds=max_attestation_age_seconds)


def open_px4_real_hardware_actuator_serial(
    *,
    serial_device: str,
    baudrate: int = 57600,
    opt_in: bool,
    attestation: PX4RealHardwarePhysicalAttestation | Mapping[str, Any],
    source_system: int = 255,
    source_component: int = 190,
    now: datetime | None = None,
    max_attestation_age_seconds: int = DEFAULT_ATTESTATION_STALENESS_SECONDS,
) -> Any:
    """Open a real pymavlink serial connection for approved actuator execution.

    Unlike the Phase-2 read-only opener, this returns the live connection with a
    send surface. That is why every validation gate runs before the lazy import
    and why callers still need ``PX4RealHardwareActuatorApproval`` before the
    backend can send.
    """

    resolved_attestation = (
        attestation
        if isinstance(attestation, PX4RealHardwarePhysicalAttestation)
        else PX4RealHardwarePhysicalAttestation.model_validate(dict(attestation))
    )
    _validate_actuator_open_inputs(
        serial_device=serial_device,
        baudrate=baudrate,
        opt_in=opt_in,
        attestation=resolved_attestation,
        now=now,
        max_attestation_age_seconds=max_attestation_age_seconds,
    )
    try:
        from pymavlink import mavutil  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise PX4RealHardwareActuatorError(
            "pymavlink is not installed; the real-hardware actuator path needs "
            "the optional 'hardware' extra: pip install '.[hardware]'"
        ) from exc
    connection = mavutil.mavlink_connection(
        serial_device,
        baud=baudrate,
        source_system=source_system,
        source_component=source_component,
    )
    return mark_connection_real_serial(connection)


def run_px4_real_hardware_arm_disarm_bench(
    *,
    store: Any,
    task_id: str,
    subject_id: str,
    approval: PX4RealHardwareActuatorApproval,
    serial_device: str | None = None,
    baudrate: int = 57600,
    opt_in: bool = False,
    connection_factory: Any | None = None,
    clock: Any | None = None,
) -> dict[str, Any]:
    """Run the first bounded bench actuator smoke: arm then disarm.

    The function owns connection lifecycle and persists both invocation records
    into the task. In tests, ``connection_factory`` provides a fake MAVLink link;
    at the bench, the real serial opener runs only with ``opt_in=True``.
    """

    if connection_factory is None:
        if serial_device is None:
            raise PX4RealHardwareActuatorError(
                "serial_device is required when no connection_factory is provided"
            )
        connection = open_px4_real_hardware_actuator_serial(
            serial_device=serial_device,
            baudrate=baudrate,
            opt_in=opt_in,
            attestation=approval.physical_attestation,
            now=clock() if callable(clock) else None,
        )
    else:
        connection = connection_factory()

    backend = PX4RealHardwareActuatorBackend(
        connection=connection,
        approval=approval,
        subject_id=subject_id,
        store=store,
        task_id=task_id,
        clock=clock or (lambda: datetime.now(timezone.utc)),
    )
    try:
        arm = backend.arm()
        disarm = backend.disarm()
        return {"arm": arm, "disarm": disarm}
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


# NOTE: ``mark_connection_real_serial`` is intentionally NOT exported. It is an
# internal seam called only by ``open_px4_real_hardware_actuator_serial`` to tag
# the live connection it just opened. It is a guardrail against accidental
# misrepresentation (e.g. the old hardcoded ``physical_execution_invoked=True``),
# not an unforgeable boundary: any in-process caller that imports it explicitly
# can still tag a fake. True provenance is anchored in the bench-time physical
# attestation, not this in-process tag. Keeping it out of ``__all__`` stops it
# looking like a public knob; tests import it explicitly to exercise the path.
__all__ = [
    "LINK_KIND_INJECTED_FAKE",
    "LINK_KIND_REAL_SERIAL_PYMAVLINK",
    "MAV_CMD_COMPONENT_ARM_DISARM",
    "MAV_CMD_DO_SET_MODE",
    "MAV_CMD_MISSION_START",
    "PX4_REAL_HARDWARE_ACTUATOR_APPROVAL_SCHEMA_VERSION",
    "PX4_REAL_HARDWARE_ACTUATOR_COMMAND_EVIDENCE_SCHEMA_VERSION",
    "PX4_REAL_HARDWARE_ACTUATOR_INVOCATION_SCHEMA_VERSION",
    "PX4RealHardwareActuatorApproval",
    "PX4RealHardwareActuatorBackend",
    "PX4RealHardwareActuatorError",
    "PX4RealHardwareActuatorInvocation",
    "PX4RealHardwareActuatorOperation",
    "PX4RealHardwareActuatorStatus",
    "build_px4_real_hardware_actuator_approval",
    "open_px4_real_hardware_actuator_serial",
    "run_px4_real_hardware_arm_disarm_bench",
]
