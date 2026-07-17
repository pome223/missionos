"""Reflex-authority MAVLink RTL dispatch on deliberation budget exhaustion.

Every existing live-MAVLink dispatch pathway in this codebase
(``px4_gazebo_emergency_dispatcher.py``'s
``PX4GazeboEmergencyCommandApproval``/``Allowlist``, and
``px4_gazebo_coupled_delivery.py``'s ``PX4GazeboCoupledCommandApproval``/
``Allowlist`` used by ``px4_mavlink_ack_state.py``) hard-requires
``operator_approval_performed=True`` as a type-level invariant and refuses
to build an allowlist without it. That invariant is correct for
operator-initiated commands and is left untouched by this module.

The reflex (``px4_recovery_reflex.py``) fires with no human in the loop —
it exists precisely because the deliberation budget ran out before a human
or the LLM could act. Reusing the operator-approval-gated pathways above
would mean minting a synthetic approval no operator actually gave, which
this codebase's design principle (claims must be truthful, no synthetic
authority) forbids. This module instead defines a separate, narrower
authority category:

- ``operator_approval_performed`` is always ``False`` here, never
  conflated with the operator-approval pathways.
- ``reflex_authority_performed`` is the honest label for what actually
  authorized the dispatch: the deterministic reflex floor, not a human.
- The only command ever dispatched is ``MAV_CMD_NAV_RETURN_TO_LAUNCH`` —
  never HOLD or LAND, which require judgment the reflex does not have.
- Dispatch only proceeds when the reflex itself reports
  ``budget_exhausted=True`` and ``entered_deliberation=False``; a live
  MAVLink opt-in flag is required in addition, mirroring every other live
  dispatch pathway's ``live_mavlink_opt_in`` gate.

This reuses the pure protocol-level MAVLink encode/decode primitives from
``px4_gazebo_emergency_dispatcher.py`` and ``px4_mavlink_ack_state.py`` —
those carry no safety-boundary opinion — but never their approval/allowlist
models.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import os
import socket
import time
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from src.runtime.px4_gazebo_emergency_dispatcher import (
    MAV_CMD_NAV_RETURN_TO_LAUNCH,
    encode_px4_gazebo_emergency_command_long,
)
from src.runtime.px4_mavlink_ack_state import (
    MAV_RESULT_ACCEPTED,
    decode_mavlink2_command_ack,
)
from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_COMMAND_ACK,
    decode_mavlink2_frame,
    encode_mavlink2_heartbeat,
)
from src.runtime.px4_recovery_reflex import PX4RecoveryReflex

PX4_RECOVERY_REFLEX_DISPATCH_SCHEMA_VERSION = (
    "missionos_px4_recovery_reflex_dispatch.v1"
)
PX4_RECOVERY_REFLEX_WATCH_SCHEMA_VERSION = (
    "missionos_px4_recovery_reflex_watch.v1"
)

PX4_REFLEX_RTL_ENABLED_ENV = "MISSIONOS_PX4_REFLEX_RTL_ENABLED"
PX4_REFLEX_RTL_ENDPOINT_PORT_ENV = "MISSIONOS_PX4_REFLEX_RTL_ENDPOINT_PORT"
PX4_REFLEX_RESERVE_LANDING_PERCENT_ENV = (
    "MISSIONOS_PX4_REFLEX_RESERVE_LANDING_PERCENT"
)
DEFAULT_REFLEX_RESERVE_LANDING_PERCENT = 25.0

DEFAULT_ACK_TIMEOUT_SECONDS = 15.0
DEFAULT_HEARTBEAT_WARMUP_FRAMES = 3
DEFAULT_HEARTBEAT_WARMUP_INTERVAL_SECONDS = 0.25


class PX4RecoveryReflexDispatchError(RuntimeError):
    """Raised when reflex RTL dispatch is invoked outside its narrow scope."""


class PX4RecoveryReflexDispatchStatus(str, Enum):
    DISPATCHED = "dispatched"
    BLOCKED = "blocked"


class PX4RecoveryReflexDispatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_RECOVERY_REFLEX_DISPATCH_SCHEMA_VERSION] = (
        PX4_RECOVERY_REFLEX_DISPATCH_SCHEMA_VERSION
    )
    dispatch_result_id: str
    dispatch_status: PX4RecoveryReflexDispatchStatus
    reflex_authority_performed: bool
    operator_approval_performed: Literal[False] = False
    # Literal[20] == MAV_CMD_NAV_RETURN_TO_LAUNCH; spelled as the literal
    # because Literal[] does not accept variable references.
    command_id: Literal[20] = MAV_CMD_NAV_RETURN_TO_LAUNCH
    command_name: Literal["MAV_CMD_NAV_RETURN_TO_LAUNCH"] = (
        "MAV_CMD_NAV_RETURN_TO_LAUNCH"
    )
    reflex_trigger: str
    endpoint_port: int
    local_bind_port: int
    mavlink_socket_opened: bool
    mavlink_frame_sent: bool
    command_ack_observed: bool
    command_ack_accepted: bool
    ack_timeout_seconds: float
    blocked_reasons: tuple[str, ...] = ()
    observed_at: datetime
    claim_boundary: str = (
        "This dispatch is authorized by the deterministic reflex floor, "
        "not an operator. It sends exactly one bounded RETURN_TO_LAUNCH "
        "command and nothing else; it is not the operator-approval-gated "
        "emergency or coupled command pathway and never claims "
        "operator_approval_performed=True."
    )
    simulation_only: Literal[True] = True
    px4_sitl_only: Literal[True] = True
    physical_actuator_execution_allowed: Literal[False] = False
    hardware_target_allowed: Literal[False] = False
    real_world_authority_granted: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    metadata: dict[str, Any] = Field(default_factory=dict)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stable_id(payload: Mapping[str, Any]) -> str:
    digest = sha256(
        json.dumps(dict(payload), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"px4_recovery_reflex_dispatch_{digest[:16]}"


def _blocked_result(
    *,
    reflex_trigger: str,
    endpoint_port: int,
    ack_timeout_seconds: float,
    blocked_reasons: tuple[str, ...],
    now: datetime | None,
) -> PX4RecoveryReflexDispatchResult:
    observed_at = _utc(now)
    payload = {
        "reflex_trigger": reflex_trigger,
        "blocked_reasons": blocked_reasons,
        "observed_at": observed_at.isoformat(),
    }
    return PX4RecoveryReflexDispatchResult(
        dispatch_result_id=_stable_id(payload),
        dispatch_status=PX4RecoveryReflexDispatchStatus.BLOCKED,
        reflex_authority_performed=False,
        reflex_trigger=reflex_trigger,
        endpoint_port=endpoint_port,
        local_bind_port=0,
        mavlink_socket_opened=False,
        mavlink_frame_sent=False,
        command_ack_observed=False,
        command_ack_accepted=False,
        ack_timeout_seconds=ack_timeout_seconds,
        blocked_reasons=blocked_reasons,
        observed_at=observed_at,
    )


def dispatch_px4_recovery_reflex_rtl(
    *,
    reflex: PX4RecoveryReflex | Mapping[str, Any],
    live_mavlink_opt_in: bool,
    endpoint_host: str = "127.0.0.1",
    endpoint_port: int = 18570,
    ack_timeout_seconds: float = DEFAULT_ACK_TIMEOUT_SECONDS,
    heartbeat_warmup_frames: int = DEFAULT_HEARTBEAT_WARMUP_FRAMES,
    heartbeat_warmup_interval_seconds: float = (
        DEFAULT_HEARTBEAT_WARMUP_INTERVAL_SECONDS
    ),
    now: datetime | None = None,
) -> PX4RecoveryReflexDispatchResult:
    """Dispatch a bounded RTL under reflex authority, or return why not.

    This only ever fires for a genuinely exhausted reflex
    (``budget_exhausted=True``, ``entered_deliberation=False``); anything
    else is a blocked result, never an exception, since a reflex evaluation
    that has not exhausted its budget is a normal, expected outcome.
    """

    reflex_model = (
        reflex
        if isinstance(reflex, PX4RecoveryReflex)
        else PX4RecoveryReflex.model_validate(dict(reflex))
    )
    blocked: list[str] = []
    if reflex_model.reflex_action != "return_to_home_recommended":
        blocked.append("reflex_action_not_return_to_home_recommended")
    if not reflex_model.budget_exhausted:
        blocked.append("reflex_budget_not_exhausted")
    if reflex_model.entered_deliberation:
        blocked.append("reflex_still_in_deliberation")
    if not live_mavlink_opt_in:
        blocked.append("live_mavlink_opt_in_not_enabled")
    if endpoint_host != "127.0.0.1":
        blocked.append("reflex_dispatch_must_target_loopback")

    if blocked:
        return _blocked_result(
            reflex_trigger=reflex_model.trigger,
            endpoint_port=endpoint_port,
            ack_timeout_seconds=ack_timeout_seconds,
            blocked_reasons=tuple(blocked),
            now=now,
        )

    heartbeat_count = max(1, int(heartbeat_warmup_frames))
    heartbeat_interval = max(0.0, float(heartbeat_warmup_interval_seconds))
    frame = encode_px4_gazebo_emergency_command_long(
        command_id=MAV_CMD_NAV_RETURN_TO_LAUNCH,
        sequence=heartbeat_count,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        local_host, local_port = sock.getsockname()
        if str(local_host) != "127.0.0.1":
            raise PX4RecoveryReflexDispatchError(
                "reflex RTL dispatch must bind from loopback"
            )
        remote = (endpoint_host, endpoint_port)
        for sequence in range(heartbeat_count):
            sock.sendto(encode_mavlink2_heartbeat(sequence=sequence), remote)
            if heartbeat_interval and sequence < heartbeat_count - 1:
                time.sleep(heartbeat_interval)
        sock.sendto(frame, remote)
        ack_observed, ack_accepted = _wait_for_reflex_rtl_ack(
            sock=sock,
            timeout_seconds=ack_timeout_seconds,
        )

    observed_at = _utc(now)
    payload = {
        "reflex_trigger": reflex_model.trigger,
        "local_bind_port": int(local_port),
        "ack_observed": ack_observed,
        "observed_at": observed_at.isoformat(),
    }
    return PX4RecoveryReflexDispatchResult(
        dispatch_result_id=_stable_id(payload),
        dispatch_status=PX4RecoveryReflexDispatchStatus.DISPATCHED,
        reflex_authority_performed=True,
        reflex_trigger=reflex_model.trigger,
        endpoint_port=endpoint_port,
        local_bind_port=int(local_port),
        mavlink_socket_opened=True,
        mavlink_frame_sent=True,
        command_ack_observed=ack_observed,
        command_ack_accepted=ack_accepted,
        ack_timeout_seconds=ack_timeout_seconds,
        observed_at=observed_at,
        metadata={"issue": 31},
    )


def _wait_for_reflex_rtl_ack(
    *,
    sock: socket.socket,
    timeout_seconds: float,
) -> tuple[bool, bool]:
    sock.settimeout(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            data, _addr = sock.recvfrom(2048)
        except socket.timeout:
            return False, False
        try:
            decoded = decode_mavlink2_frame(data)
        except Exception:
            continue
        if decoded["msg_id"] != MAVLINK_MSG_ID_COMMAND_ACK:
            continue
        try:
            ack = decode_mavlink2_command_ack(data)
        except Exception:
            continue
        if int(ack["command_id"]) != MAV_CMD_NAV_RETURN_TO_LAUNCH:
            continue
        return True, int(ack["result_code"]) == MAV_RESULT_ACCEPTED
    return False, False


def default_px4_reflex_dispatcher_from_env() -> (
    Callable[[Mapping[str, Any]], dict[str, Any]] | None
):
    """Env-gated production dispatcher for reflex-exhaustion RTL.

    Returns None — record only, dispatch nothing — unless
    ``MISSIONOS_PX4_REFLEX_RTL_ENABLED=1``. Setting that env var is the
    explicit live-MAVLink opt-in for this pathway, mirroring the
    ``live_mavlink_opt_in`` gate every other live dispatch path requires.
    """

    if os.environ.get(PX4_REFLEX_RTL_ENABLED_ENV, "") != "1":
        return None
    raw_port = os.environ.get(PX4_REFLEX_RTL_ENDPOINT_PORT_ENV, "").strip()
    endpoint_port = int(raw_port) if raw_port else 18570

    def _dispatch(reflex: Mapping[str, Any]) -> dict[str, Any]:
        return dispatch_px4_recovery_reflex_rtl(
            reflex=reflex,
            live_mavlink_opt_in=True,
            endpoint_port=endpoint_port,
        ).model_dump(mode="json")

    return _dispatch


def reflex_reserve_landing_percent_from_env() -> float:
    raw = os.environ.get(PX4_REFLEX_RESERVE_LANDING_PERCENT_ENV, "").strip()
    if not raw:
        return DEFAULT_REFLEX_RESERVE_LANDING_PERCENT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_REFLEX_RESERVE_LANDING_PERCENT


def watch_px4_recovery_reflex_from_battery(
    *,
    battery_remaining_percent: float,
    reserve_landing_percent: float | None = None,
    dispatcher: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    already_dispatched: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate the reflex against a live battery reading; dispatch if due.

    This is the budget's enforcement point for live SITL flights: the
    polling loop feeds each observed battery percent here, and the moment
    the margin over the landing reserve is exhausted, the (env-opted-in)
    dispatcher sends one bounded RTL. With no dispatcher configured the
    watch stays record-only and says so.
    """

    from src.runtime.px4_recovery_reflex import build_px4_recovery_reflex

    reserve = (
        reserve_landing_percent
        if reserve_landing_percent is not None
        else reflex_reserve_landing_percent_from_env()
    )
    reflex = build_px4_recovery_reflex(
        trigger="live_battery_below_reserve_landing_threshold"
        if battery_remaining_percent <= reserve
        else "live_battery_watch",
        battery_percent=float(battery_remaining_percent),
        reserve_landing_percent=float(reserve),
        now=now,
    )
    record: dict[str, Any] = {
        "schema_version": PX4_RECOVERY_REFLEX_WATCH_SCHEMA_VERSION,
        "reflex": reflex.model_dump(mode="json"),
        "dispatch": {},
        "watch_status": "record_only",
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
    }
    if not reflex.budget_exhausted:
        record["watch_status"] = "within_budget"
        return record
    if already_dispatched:
        record["watch_status"] = "already_dispatched"
        return record
    if dispatcher is None:
        record["watch_status"] = "exhausted_record_only"
        return record
    record["dispatch"] = dict(dispatcher(reflex.model_dump(mode="json")))
    record["watch_status"] = "dispatched"
    return record


__all__ = [
    "DEFAULT_ACK_TIMEOUT_SECONDS",
    "DEFAULT_HEARTBEAT_WARMUP_FRAMES",
    "DEFAULT_HEARTBEAT_WARMUP_INTERVAL_SECONDS",
    "DEFAULT_REFLEX_RESERVE_LANDING_PERCENT",
    "PX4_RECOVERY_REFLEX_DISPATCH_SCHEMA_VERSION",
    "PX4_RECOVERY_REFLEX_WATCH_SCHEMA_VERSION",
    "PX4_REFLEX_RESERVE_LANDING_PERCENT_ENV",
    "PX4_REFLEX_RTL_ENABLED_ENV",
    "PX4_REFLEX_RTL_ENDPOINT_PORT_ENV",
    "PX4RecoveryReflexDispatchError",
    "PX4RecoveryReflexDispatchResult",
    "PX4RecoveryReflexDispatchStatus",
    "default_px4_reflex_dispatcher_from_env",
    "dispatch_px4_recovery_reflex_rtl",
    "reflex_reserve_landing_percent_from_env",
    "watch_px4_recovery_reflex_from_battery",
]
