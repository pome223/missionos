"""Real-bench pymavlink reader for the PX4 read-only observation backend.

This is the **Phase 2** companion to
:mod:`src.runtime.px4_real_hardware_readonly_target`. Phase 1 added the
read-only backend behind the existing ``PX4MissionTarget`` Protocol with the
telemetry source *injected* as a reader callable. This module supplies the one
concrete reader that talks to a real USB-serial Pixhawk over MAVLink — and
nothing else changes: the backend, Core, and the Agent-facing verbs stay
byte-for-byte the same. The environment difference is absorbed *here*, in the
reader, exactly as the design requires.

Three structural guarantees, none of them flag-based
----------------------------------------------------

1. **Read-only on the wire.** The live link is wrapped in
   :class:`ReadOnlyMavlinkConnection`, which exposes only ``recv_match`` /
   ``recv_msg`` / ``wait_heartbeat`` / ``close`` and *raises* on every send
   path (``write``, ``mav.send``, any ``mav.*_send``). The reader is
   structurally incapable of putting a byte on the wire, so "passive listener"
   is enforced, not promised.

2. **Serial only, never a network target.** ``open_*`` refuses anything that is
   not a local ``/dev/tty*`` / ``/dev/cu*`` serial device. A UDP/TCP endpoint
   (which would let this point at a live SITL or a remote vehicle) cannot pass
   the device check, so this path can only ever observe the bench in front of
   the operator.

3. **Physical attestation that cannot lie.** Opening the link requires a
   :class:`PX4RealHardwarePhysicalAttestation` whose safety fields are
   ``Literal[True]`` — you cannot construct one that says the propellers are
   still on. No attestation, or a stale one, means no connection. The backend
   itself never fabricates this; a human supplies it at the bench.

pymavlink is **lazy-imported**: nothing here imports it at module load, and the
import happens only *after* every validation passes inside
:func:`open_px4_real_hardware_readonly_serial`. The pure mapper
(:func:`mavlink_message_to_reading`) and the read-only wrapper are therefore
fully testable offline, with no hardware, no serial device, and no pymavlink
installed. Install the optional dependency with ``pip install '.[hardware]'``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from src.runtime.hil_telemetry_contract import (
    HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
    HilTelemetryContract,
)
from src.runtime.px4_real_hardware_readonly_target import (
    PX4_REAL_HARDWARE_READONLY_CONTRACT_ID,
    PX4_REAL_HARDWARE_READONLY_SUBJECT_KIND,
    PX4RealHardwareReadOnlyBackend,
    attach_px4_real_hardware_readonly_observation_to_task,
    build_px4_real_hardware_readonly_contract,
)

# MAVLink message types this read-only path understands. Anything else recv'd
# is ignored (not an error): the bench may emit dozens of other types.
READ_ONLY_OBSERVATION_MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "HEARTBEAT",
        "SYS_STATUS",
        "BATTERY_STATUS",
        "GPS_RAW_INT",
        "GLOBAL_POSITION_INT",
        "ATTITUDE",
    }
)

# HEARTBEAT.base_mode bit that means the vehicle is armed.
MAV_MODE_FLAG_SAFETY_ARMED = 128

# GPS_RAW_INT.fix_type -> human label.
_GPS_FIX_TYPE_LABELS: dict[int, str] = {
    0: "no_gps",
    1: "no_fix",
    2: "fix_2d",
    3: "fix_3d",
    4: "dgps",
    5: "rtk_float",
    6: "rtk_fixed",
}

# Baud rates a PX4 USB/telemetry link actually runs at. A typo here should fail
# closed rather than silently open a mis-configured link.
SUPPORTED_BAUDRATES: frozenset[int] = frozenset({57600, 115200, 921600})

# A real bench device is a local serial port, never a network endpoint. The
# pattern forbids ':' so no ``udpin:``/``tcp:`` string can slip through.
_SERIAL_DEVICE_PATTERN = re.compile(r"^/dev/(tty|cu)[A-Za-z0-9._-]*$")

# How old a physical attestation may be before the operator must re-attest.
DEFAULT_ATTESTATION_STALENESS_SECONDS = 900

# Sentinel used by SYS_STATUS / BATTERY_STATUS for "unknown".
_BATTERY_REMAINING_UNKNOWN = -1
_BATTERY_CELL_UNUSED_MV = 65535


class PX4RealHardwareReaderError(RuntimeError):
    """Raised when the real-hardware read-only reader cannot proceed safely."""


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class PX4RealHardwarePhysicalAttestation(BaseModel):
    """Operator's physical-safety attestation for a real-bench session.

    The safety fields are ``Literal[True]``: Pydantic refuses to construct an
    attestation that claims anything *other* than propellers-removed and an
    operator physically present. There is no way to express "propellers still
    on" — so the open path simply cannot run without the physically safe state
    having been asserted by a named human at a known time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    propellers_removed: Literal[True]
    operator_physically_present: Literal[True]
    attesting_operator_id: str
    attested_at: datetime
    bench_photo_evidence_ref: str | None = None

    def ensure_fresh(
        self,
        *,
        now: datetime | None = None,
        max_age_seconds: int = DEFAULT_ATTESTATION_STALENESS_SECONDS,
    ) -> None:
        """Raise if the attestation is stale or post-dated.

        A safety attestation describes the bench *at attestation time*. If it is
        older than ``max_age_seconds`` the operator must look again and re-attest;
        if it is in the future it is not trustworthy either.
        """

        moment = _utc(now)
        attested = _utc(self.attested_at)
        age = (moment - attested).total_seconds()
        if age < 0:
            raise PX4RealHardwareReaderError(
                "physical attestation is post-dated "
                f"(attested_at={attested.isoformat()} > now={moment.isoformat()})"
            )
        if age > max_age_seconds:
            raise PX4RealHardwareReaderError(
                f"physical attestation is stale: {age:.0f}s old "
                f"(max {max_age_seconds}s) — operator must re-attest the bench"
            )


def _coerce_attestation(
    value: PX4RealHardwarePhysicalAttestation | dict[str, Any],
) -> PX4RealHardwarePhysicalAttestation:
    if isinstance(value, PX4RealHardwarePhysicalAttestation):
        return value
    return PX4RealHardwarePhysicalAttestation.model_validate(value)


class ReadOnlyMavlinkConnection:
    """A send-blocking wrapper around a live pymavlink connection.

    Receive verbs (``recv_match`` / ``recv_msg`` / ``wait_heartbeat``) and
    ``close`` pass straight through. Every send path raises and is counted in
    :attr:`blocked_send_attempts`:

    - ``write(...)`` — raw byte send
    - ``mav.send(...)`` and any ``mav.*_send(...)`` — encoded-frame send,
      intercepted by :class:`_ReadOnlyMavProxy`

    This is defense-in-depth: the reader never *calls* a send path, and this
    wrapper makes one structurally impossible even if buggy code tried.
    """

    def __init__(self, raw_connection: Any) -> None:
        self._raw = raw_connection
        self.blocked_send_attempts: list[str] = []
        self._mav_proxy = _ReadOnlyMavProxy(
            getattr(raw_connection, "mav", None), self._record_block
        )

    def _record_block(self, name: str) -> None:
        self.blocked_send_attempts.append(name)

    @property
    def mav(self) -> "_ReadOnlyMavProxy":
        return self._mav_proxy

    def recv_match(self, *args: Any, **kwargs: Any) -> Any:
        return self._raw.recv_match(*args, **kwargs)

    def recv_msg(self, *args: Any, **kwargs: Any) -> Any:
        return self._raw.recv_msg(*args, **kwargs)

    def wait_heartbeat(self, *args: Any, **kwargs: Any) -> Any:
        # wait_heartbeat only *receives*; it does not transmit a heartbeat.
        return self._raw.wait_heartbeat(*args, **kwargs)

    def write(self, *_args: Any, **_kwargs: Any) -> Any:
        self._record_block("write")
        raise PX4RealHardwareReaderError(
            "ReadOnlyMavlinkConnection refuses write: read-only link cannot transmit"
        )

    def close(self) -> None:
        close = getattr(self._raw, "close", None)
        if callable(close):
            close()


class _ReadOnlyMavProxy:
    """Proxy over a pymavlink ``MAVLink`` object that blocks all sends.

    Any attribute named ``send`` or ending in ``_send`` returns a callable that
    raises; everything else (encoders, ``seq``, dialect tables) is read through
    so callers can still *build* messages — they just cannot transmit them.
    """

    def __init__(self, mav: Any, on_block: Callable[[str], None]) -> None:
        self._mav = mav
        self._on_block = on_block

    def __getattr__(self, name: str) -> Any:
        if name == "send" or name.endswith("_send"):
            def _blocked(*_args: Any, **_kwargs: Any) -> Any:
                self._on_block(name)
                raise PX4RealHardwareReaderError(
                    f"ReadOnlyMavlinkConnection refuses mav.{name}: "
                    "read-only link cannot transmit"
                )

            return _blocked
        if self._mav is None:
            raise PX4RealHardwareReaderError(
                "underlying MAVLink connection exposes no .mav object"
            )
        return getattr(self._mav, name)


def _message_field(msg: Any, name: str) -> Any:
    """Read a field off a pymavlink message or a dict-shaped stand-in."""

    if isinstance(msg, dict):
        return msg.get(name)
    return getattr(msg, name, None)


def _message_type(msg: Any) -> str | None:
    get_type = getattr(msg, "get_type", None)
    if callable(get_type):
        return get_type()
    if isinstance(msg, dict):
        value = msg.get("mavpackettype") or msg.get("type")
        return str(value) if value is not None else None
    return None


def _battery_voltage_v_from_cells(voltages: Iterable[Any] | None) -> float | None:
    """Sum the valid per-cell millivolts of a BATTERY_STATUS into volts."""

    if not voltages:
        return None
    total_mv = 0
    saw_cell = False
    for cell in voltages:
        cell_mv = int(cell)
        if cell_mv == _BATTERY_CELL_UNUSED_MV:
            continue
        total_mv += cell_mv
        saw_cell = True
    if not saw_cell:
        return None
    return round(total_mv / 1000.0, 3)


def _measurements_for(msg: Any, msg_type: str) -> dict[str, float | int | bool | str]:
    """Map one understood MAVLink message to read-only scalar measurements.

    Pure and duck-typed: ``msg`` may be a real pymavlink message (attribute
    access + ``get_type()``) or a dict / ``SimpleNamespace`` stand-in, so this
    is fully exercisable without pymavlink installed. Only telemetry scalars are
    emitted — never a command-, action-, or dispatch-shaped key.
    """

    out: dict[str, float | int | bool | str] = {}
    if msg_type == "HEARTBEAT":
        base_mode = _message_field(msg, "base_mode")
        if base_mode is not None:
            out["base_mode"] = int(base_mode)
            out["armed"] = bool(int(base_mode) & MAV_MODE_FLAG_SAFETY_ARMED)
        system_status = _message_field(msg, "system_status")
        if system_status is not None:
            out["system_status"] = int(system_status)
    elif msg_type == "SYS_STATUS":
        voltage_mv = _message_field(msg, "voltage_battery")
        if voltage_mv is not None and int(voltage_mv) > 0:
            out["battery_voltage_v"] = round(int(voltage_mv) / 1000.0, 3)
        remaining = _message_field(msg, "battery_remaining")
        if remaining is not None and int(remaining) != _BATTERY_REMAINING_UNKNOWN:
            out["battery_remaining_pct"] = int(remaining)
    elif msg_type == "BATTERY_STATUS":
        voltage_v = _battery_voltage_v_from_cells(_message_field(msg, "voltages"))
        if voltage_v is not None:
            out["battery_voltage_v"] = voltage_v
        remaining = _message_field(msg, "battery_remaining")
        if remaining is not None and int(remaining) != _BATTERY_REMAINING_UNKNOWN:
            out["battery_remaining_pct"] = int(remaining)
    elif msg_type == "GPS_RAW_INT":
        fix_type = _message_field(msg, "fix_type")
        if fix_type is not None:
            out["gps_fix"] = _GPS_FIX_TYPE_LABELS.get(int(fix_type), "unknown")
            out["gps_fix_type"] = int(fix_type)
        satellites = _message_field(msg, "satellites_visible")
        if satellites is not None:
            out["satellites_visible"] = int(satellites)
    elif msg_type == "GLOBAL_POSITION_INT":
        relative_alt = _message_field(msg, "relative_alt")
        if relative_alt is not None:
            out["relative_alt_m"] = round(int(relative_alt) / 1000.0, 3)
        alt = _message_field(msg, "alt")
        if alt is not None:
            out["alt_m"] = round(int(alt) / 1000.0, 3)
    elif msg_type == "ATTITUDE":
        for axis in ("roll", "pitch", "yaw"):
            value = _message_field(msg, axis)
            if value is not None:
                out[f"{axis}_rad"] = round(float(value), 6)
    return out


def mavlink_message_to_reading(
    msg: Any,
    *,
    contract_id: str = PX4_REAL_HARDWARE_READONLY_CONTRACT_ID,
    subject_kind: str = PX4_REAL_HARDWARE_READONLY_SUBJECT_KIND,
    subject_id: str,
    captured_at: datetime,
    link_label: str = "usb_serial",
) -> dict[str, Any] | None:
    """Turn one MAVLink message into a read-only HIL envelope dict, or ``None``.

    Returns ``None`` for any message type outside
    :data:`READ_ONLY_OBSERVATION_MESSAGE_TYPES` or any understood type that
    carried no usable field — the caller skips those. The returned dict is shaped
    for :func:`ingest_hil_telemetry_envelope`; it carries only telemetry scalars
    plus benign provenance metadata, never a command-like key.
    """

    msg_type = _message_type(msg)
    if msg_type is None or msg_type not in READ_ONLY_OBSERVATION_MESSAGE_TYPES:
        return None
    measurements = _measurements_for(msg, msg_type)
    if not measurements:
        return None
    metadata: dict[str, Any] = {
        "link": link_label,
        "mavlink_message_type": msg_type,
    }
    source_system = _message_field(msg, "get_srcSystem")
    if callable(source_system):
        metadata["source_system"] = int(source_system())
    return {
        "schema_version": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
        "contract_id": contract_id,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "captured_at": _utc(captured_at).isoformat(),
        "measurements": measurements,
        "metadata": metadata,
    }


@dataclass
class PX4RealHardwareSerialReader:
    """A ``TelemetryReader`` that reads one merged telemetry snapshot per call.

    Each ``__call__`` drains up to ``max_messages_per_read`` understood messages
    off the read-only link (bounded by ``recv_timeout_seconds`` per ``recv_match``)
    and merges their measurements into a single envelope dict, stamped with the
    injected ``clock``. A single HEARTBEAT only knows ``armed``; merging a short
    burst yields a fuller snapshot (battery + GPS + attitude) in one envelope.

    The reader holds a :class:`ReadOnlyMavlinkConnection`, so it is structurally
    incapable of transmitting. It never calls a send path.
    """

    connection: ReadOnlyMavlinkConnection
    subject_id: str
    contract_id: str = PX4_REAL_HARDWARE_READONLY_CONTRACT_ID
    subject_kind: str = PX4_REAL_HARDWARE_READONLY_SUBJECT_KIND
    message_types: frozenset[str] = READ_ONLY_OBSERVATION_MESSAGE_TYPES
    recv_timeout_seconds: float = 1.0
    max_messages_per_read: int = 12
    link_label: str = "usb_serial"
    clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc)
    )

    def __call__(self) -> dict[str, Any]:
        merged: dict[str, float | int | bool | str] = {}
        metadata: dict[str, Any] = {"link": self.link_label, "merged_message_types": []}
        seen_types: list[str] = []
        for _ in range(max(1, self.max_messages_per_read)):
            msg = self.connection.recv_match(
                type=sorted(self.message_types),
                blocking=True,
                timeout=self.recv_timeout_seconds,
            )
            if msg is None:
                break
            msg_type = _message_type(msg)
            if msg_type is None or msg_type not in self.message_types:
                continue
            measurements = _measurements_for(msg, msg_type)
            if not measurements:
                continue
            merged.update(measurements)
            if msg_type not in seen_types:
                seen_types.append(msg_type)
        if not merged:
            raise PX4RealHardwareReaderError(
                "no understood MAVLink telemetry within "
                f"{self.max_messages_per_read} frames / "
                f"{self.recv_timeout_seconds}s window"
            )
        metadata["merged_message_types"] = seen_types
        return {
            "schema_version": HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION,
            "contract_id": self.contract_id,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "captured_at": _utc(self.clock()).isoformat(),
            "measurements": merged,
            "metadata": metadata,
        }


def _validate_open_inputs(
    *,
    serial_device: str,
    baudrate: int,
    opt_in: bool,
    attestation: PX4RealHardwarePhysicalAttestation,
    now: datetime | None,
    max_attestation_age_seconds: int,
) -> None:
    """All gates that must pass *before* pymavlink is imported or a port opened."""

    if opt_in is not True:
        raise PX4RealHardwareReaderError(
            "real-hardware read-only observation requires explicit opt_in=true"
        )
    if not _SERIAL_DEVICE_PATTERN.match(serial_device):
        raise PX4RealHardwareReaderError(
            "real-hardware read-only observation is restricted to a local serial "
            f"device (/dev/tty* or /dev/cu*); refused {serial_device!r}"
        )
    if baudrate not in SUPPORTED_BAUDRATES:
        raise PX4RealHardwareReaderError(
            f"unsupported baudrate {baudrate}; expected one of "
            f"{sorted(SUPPORTED_BAUDRATES)}"
        )
    attestation.ensure_fresh(now=now, max_age_seconds=max_attestation_age_seconds)


def open_px4_real_hardware_readonly_serial(
    *,
    serial_device: str,
    baudrate: int = 57600,
    opt_in: bool,
    attestation: PX4RealHardwarePhysicalAttestation | dict[str, Any],
    source_system: int = 255,
    source_component: int = 190,
    now: datetime | None = None,
    max_attestation_age_seconds: int = DEFAULT_ATTESTATION_STALENESS_SECONDS,
) -> ReadOnlyMavlinkConnection:
    """Open a passive, read-only MAVLink listener on a local serial Pixhawk.

    Every gate in :func:`_validate_open_inputs` runs **before** pymavlink is
    imported: ``opt_in`` must be ``True``, the device must be a local serial
    port (never a network endpoint), the baud must be supported, and a fresh
    physical attestation must be supplied. Only then is ``pymavlink`` lazily
    imported and a connection opened, immediately wrapped in
    :class:`ReadOnlyMavlinkConnection` so it cannot transmit.

    Raises :class:`PX4RealHardwareReaderError` if pymavlink is not installed —
    install the optional dependency with ``pip install '.[hardware]'``.
    """

    resolved_attestation = _coerce_attestation(attestation)
    _validate_open_inputs(
        serial_device=serial_device,
        baudrate=baudrate,
        opt_in=opt_in,
        attestation=resolved_attestation,
        now=now,
        max_attestation_age_seconds=max_attestation_age_seconds,
    )

    try:
        from pymavlink import mavutil  # noqa: PLC0415  (lazy: opt-in hardware path only)
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise PX4RealHardwareReaderError(
            "pymavlink is not installed; the real-hardware read-only path needs "
            "the optional 'hardware' extra: pip install '.[hardware]'"
        ) from exc

    raw = mavutil.mavlink_connection(
        serial_device,
        baud=baudrate,
        source_system=source_system,
        source_component=source_component,
    )
    return ReadOnlyMavlinkConnection(raw)


ConnectionFactory = Callable[[], ReadOnlyMavlinkConnection]


def run_px4_real_hardware_readonly_observation(
    *,
    store: Any,
    task_id: str,
    subject_id: str,
    serial_device: str | None = None,
    baudrate: int = 57600,
    opt_in: bool = False,
    attestation: PX4RealHardwarePhysicalAttestation | dict[str, Any] | None = None,
    sample_count: int = 3,
    recv_timeout_seconds: float = 1.0,
    max_messages_per_read: int = 12,
    contract: HilTelemetryContract | None = None,
    connection_factory: ConnectionFactory | None = None,
    clock: Callable[[], datetime] | None = None,
    now: datetime | None = None,
    max_attestation_age_seconds: int = DEFAULT_ATTESTATION_STALENESS_SECONDS,
) -> dict[str, Any]:
    """Open -> observe ``sample_count`` snapshots -> attach to a task -> close.

    The orchestrator the opt-in real-serial smoke calls. It wires the concrete
    serial reader into the *unchanged* :class:`PX4RealHardwareReadOnlyBackend`
    and persists the result through the *unchanged* attach helper, so Phase 2
    adds no new persistence or Core surface — only a reader.

    ``connection_factory`` is the offline seam: pass one returning a
    :class:`ReadOnlyMavlinkConnection` over a scripted fake link to exercise the
    whole pipeline with no hardware and no pymavlink. When it is ``None`` the
    real :func:`open_px4_real_hardware_readonly_serial` path runs, which requires
    ``serial_device``, ``opt_in=True`` and a fresh ``attestation``.
    """

    if sample_count < 1:
        raise PX4RealHardwareReaderError("sample_count must be >= 1")

    resolved_contract = contract or build_px4_real_hardware_readonly_contract()

    if connection_factory is not None:
        connection = connection_factory()
    else:
        if serial_device is None:
            raise PX4RealHardwareReaderError(
                "serial_device is required when no connection_factory is provided"
            )
        if attestation is None:
            raise PX4RealHardwareReaderError(
                "a physical attestation is required to open a real serial link"
            )
        connection = open_px4_real_hardware_readonly_serial(
            serial_device=serial_device,
            baudrate=baudrate,
            opt_in=opt_in,
            attestation=attestation,
            now=now,
            max_attestation_age_seconds=max_attestation_age_seconds,
        )

    reader = PX4RealHardwareSerialReader(
        connection=connection,
        subject_id=subject_id,
        contract_id=resolved_contract.contract_id,
        subject_kind=resolved_contract.subject_kind,
        recv_timeout_seconds=recv_timeout_seconds,
        max_messages_per_read=max_messages_per_read,
        clock=clock or (lambda: datetime.now(timezone.utc)),
    )

    backend = PX4RealHardwareReadOnlyBackend(
        telemetry_reader=reader,
        contract=resolved_contract,
    )

    try:
        observations = [backend.observe() for _ in range(sample_count)]
    finally:
        connection.close()

    return attach_px4_real_hardware_readonly_observation_to_task(
        store=store,
        task_id=task_id,
        contract=resolved_contract,
        observations=observations,
    )


__all__ = [
    "DEFAULT_ATTESTATION_STALENESS_SECONDS",
    "MAV_MODE_FLAG_SAFETY_ARMED",
    "READ_ONLY_OBSERVATION_MESSAGE_TYPES",
    "SUPPORTED_BAUDRATES",
    "ConnectionFactory",
    "PX4RealHardwarePhysicalAttestation",
    "PX4RealHardwareReaderError",
    "PX4RealHardwareSerialReader",
    "ReadOnlyMavlinkConnection",
    "mavlink_message_to_reading",
    "open_px4_real_hardware_readonly_serial",
    "run_px4_real_hardware_readonly_observation",
]
