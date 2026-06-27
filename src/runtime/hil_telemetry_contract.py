"""Hardware-in-the-loop (HIL) telemetry-only contract — design slice (#172).

HIL means hardware-in-the-loop. In this slice it is telemetry-only and
read-only: an ingestion path that accepts telemetry from a real or
real-equivalent subject and **refuses any payload that looks like a command,
action, actuator dispatch, or live / physical execution flag**. Action /
command / dispatch fields are not optional-and-False here — they are
**not expressible** in the envelope schema, and known command-like keys are
fail-closed at ingestion time.

Why a separate type from ``simulator_adapter_contract.v1``
----------------------------------------------------------

``SimulatorAdapterContract`` describes a simulator: it has an
``action_schema``, an ``episode_schema``, a ``replay_trace_schema``, and
runs dry-run plans. HIL telemetry is the opposite surface: real (or
real-equivalent) hardware sends telemetry, the operator does not send
actions back. Adding a ``telemetry_only`` mode to the simulator contract
would force ``action_schema`` to be made optional, weakening that contract's
existing invariants. A separate type keeps the boundary physical instead of
flag-based.

Out of scope for this PR
------------------------

- toy-grid HIL builder (toy-grid is a simulator, not hardware)
- runtime / gate / scorecard integration
- UI / mission API
- real hardware connection (PX4, ROS, MAVLink, ...)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION = "hil_telemetry_contract.v1"
HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION = "hil_telemetry_envelope.v1"


# Known command-like keys we never want to see on the telemetry path.
_FORBIDDEN_COMMAND_KEYS: frozenset[str] = frozenset(
    {
        "action",
        "actions",
        "command",
        "commands",
        "actuator",
        "actuators",
        "dispatch",
        "ros_topic",
        "ros2_topic",
        "execute",
        "execute_now",
        "live_execution_allowed",
        "physical_execution_invoked",
    }
)


def _normalize_forbidden_key(key: str) -> str:
    """Normalize casing/separators so common key variants compare equally."""

    return re.sub(r"[^a-z0-9]", "", key.lower())


_FORBIDDEN_COMMAND_KEYS_NORMALIZED: frozenset[str] = frozenset(
    _normalize_forbidden_key(key) for key in _FORBIDDEN_COMMAND_KEYS
)


class HilTelemetryMode(str, Enum):
    """The HIL execution mode the contract advertises.

    Only ``telemetry_only`` exists today. Future read-only stronger modes
    (e.g. operator-confirmed limited writes) should add new values, with
    their own ``Literal`` invariants and their own ingestion path.
    """

    TELEMETRY_ONLY = "telemetry_only"


class HilTelemetryRejected(ValueError):
    """Raised when a candidate HIL telemetry payload fails ingestion checks.

    This is a fail-closed signal: any rejection means the payload was not
    accepted into the telemetry path. Callers must not retry with the
    same payload — they must produce one that complies with the envelope
    schema and contains no command-like fields.
    """


class HilTelemetryEnvelope(BaseModel):
    """Read-only telemetry envelope accepted on the HIL ingestion path.

    The shape is intentionally narrow: there is no ``action`` /
    ``command`` / ``actuator`` / ``dispatch`` field, and ``measurements``
    only carries scalar values (``float | int | bool | str``). Unknown
    fields are rejected by Pydantic ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION] = (
        HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION
    )
    contract_id: str
    subject_kind: str
    subject_id: str
    captured_at: datetime
    measurements: dict[str, float | int | bool | str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HilTelemetryContract(BaseModel):
    """Static description of one HIL telemetry-only ingestion endpoint.

    Capability flags use ``Literal[False]`` and ``operator_approval_required``
    uses ``Literal[True]`` so Pydantic refuses to construct a contract that
    advertises any action / command / live / physical / ROS dispatch
    capability through this slice. Any future endpoint that needs even a
    bounded write path must come with its own contract version (v2+) and
    its own approval / policy story.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION] = (
        HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION
    )
    contract_id: str
    subject_kind: str
    telemetry_envelope_schema: str
    supports_action_dispatch: Literal[False] = False
    supports_command_payload: Literal[False] = False
    supports_live_execution: Literal[False] = False
    supports_physical_execution: Literal[False] = False
    supports_ros_dispatch: Literal[False] = False
    operator_approval_required: Literal[True] = True
    mode: Literal[HilTelemetryMode.TELEMETRY_ONLY] = HilTelemetryMode.TELEMETRY_ONLY


def _command_like_keys(value: Any, *, _path: str = "") -> list[str]:
    """Walk a payload and return the dotted paths of any command-like keys.

    The check is case-insensitive and separator-insensitive against
    ``_FORBIDDEN_COMMAND_KEYS_NORMALIZED``. Lists are walked because a
    command-like field could otherwise hide as ``measurements.foo.0.command``.
    """

    findings: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if (
                isinstance(key, str)
                and _normalize_forbidden_key(key) in _FORBIDDEN_COMMAND_KEYS_NORMALIZED
            ):
                findings.append(f"{_path}{key}" if _path else key)
            sub_path = f"{_path}{key}." if _path else f"{key}."
            findings.extend(_command_like_keys(sub, _path=sub_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            sub_path = f"{_path}{index}." if _path else f"{index}."
            findings.extend(_command_like_keys(item, _path=sub_path))
    return findings


def ingest_hil_telemetry_envelope(
    payload: HilTelemetryEnvelope | dict[str, Any],
) -> HilTelemetryEnvelope:
    """Validate and accept a HIL telemetry payload.

    The check has two layers:

    1. **Command-like-key pre-check** (case-insensitive, recursive). Any
       ``action`` / ``command`` / ``actuator`` / ``dispatch`` /
       ``ros_topic`` / ``ros2_topic`` / ``execute`` / ``execute_now`` /
       ``live_execution_allowed`` / ``physical_execution_invoked`` key
       found anywhere in the payload — top-level, nested in
       ``measurements`` or ``metadata``, or further inside a list — fails
       closed with ``HilTelemetryRejected``. The error message surfaces
       the dotted paths so the caller can see which keys offended.

    2. **Pydantic validation** against ``HilTelemetryEnvelope`` (the
       envelope is ``extra="forbid"``, so any other unknown field also
       fails closed).

    The two layers are complementary: pre-check gives a clear rejection
    message specifically for command-like content, and Pydantic catches
    everything else. Either layer rejecting means the payload is not
    accepted.
    """

    if isinstance(payload, HilTelemetryEnvelope):
        return payload
    if not isinstance(payload, dict):
        raise HilTelemetryRejected(
            "HIL telemetry payload must be a dict or HilTelemetryEnvelope; "
            f"got {type(payload).__name__}"
        )

    forbidden_paths = _command_like_keys(payload)
    if forbidden_paths:
        raise HilTelemetryRejected(
            "HIL telemetry path refused payload carrying command-like keys: "
            + ", ".join(sorted(forbidden_paths))
        )

    try:
        return HilTelemetryEnvelope.model_validate(payload)
    except ValidationError as exc:
        raise HilTelemetryRejected(
            f"invalid HIL telemetry envelope: {exc}"
        ) from exc


__all__ = [
    "HIL_TELEMETRY_CONTRACT_SCHEMA_VERSION",
    "HIL_TELEMETRY_ENVELOPE_SCHEMA_VERSION",
    "HilTelemetryContract",
    "HilTelemetryEnvelope",
    "HilTelemetryMode",
    "HilTelemetryRejected",
    "ingest_hil_telemetry_envelope",
]
