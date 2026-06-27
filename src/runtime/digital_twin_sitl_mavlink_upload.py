"""Digital Twin SITL-only MAVLink mission upload receipt."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import socket
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.digital_twin_mission_environment import (
    DigitalTwinMissionEnvironmentError,
    DigitalTwinPx4MissionItemCandidate,
    digital_twin_px4_mission_item_candidate_ref,
)
from src.runtime.digital_twin_sitl_process_runner import (
    DigitalTwinSITLProcessRun,
    digital_twin_sitl_process_run_ref,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_FRAME_GLOBAL_INT,
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_TAKEOFF,
    MAV_CMD_NAV_WAYPOINT,
    MAV_MISSION_ACCEPTED,
    PX4GazeboSITLMissionItem,
    decode_mavlink2_mission_ack_type,
    decode_mavlink2_mission_request_int,
    encode_mavlink2_mission_count,
    encode_mavlink2_mission_item_int,
)
from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_HEARTBEAT,
    decode_mavlink2_frame,
    encode_mavlink2_heartbeat,
    encode_mavlink2_request_message,
)


DIGITAL_TWIN_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION = (
    "digital_twin_sitl_mission_upload_receipt.v1"
)
DIGITAL_TWIN_SITL_MISSION_UPLOAD_OPT_IN_ENV = (
    "RUN_DIGITAL_TWIN_SITL_MISSION_UPLOAD"
)
DIGITAL_TWIN_SITL_EXTRA_ENDPOINTS_ENV = (
    "DIGITAL_TWIN_SITL_ALLOWLISTED_ENDPOINTS"
)
DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT = "udp://127.0.0.1:14540"
DEFAULT_DIGITAL_TWIN_SITL_ENDPOINTS = (
    "udp://127.0.0.1:14540",
    "udp://127.0.0.1:14550",
)

class DigitalTwinSITLMAVLinkUploadError(RuntimeError):
    """Raised when Digital Twin SITL upload cannot proceed safely."""


class DigitalTwinSITLMissionUploader(Protocol):
    def upload(
        self,
        *,
        items: Sequence[PX4GazeboSITLMissionItem],
        target_endpoint: str,
        timeout_seconds: float,
    ) -> tuple[tuple[int, ...], int]:
        ...


class DigitalTwinSITLMissionUploadReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION] = (
        DIGITAL_TWIN_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str
    digital_twin_px4_mission_item_candidate_ref: str
    digital_twin_sitl_process_run_ref: str
    target_endpoint: str
    mission_upload_attempted: bool
    px4_mission_upload_allowed: bool
    mavlink_dispatch_performed: bool
    mission_upload_observed: bool
    mission_ack_observed: bool
    mission_ack_type: int | None = None
    mission_request_sequences: tuple[int, ...] = ()
    telemetry_observed: bool
    heartbeat_observed: bool
    position_observed: bool = False
    same_run_binding_ref: str
    candidate_item_count: int = Field(ge=0)
    mission_items_source: Literal["fixture_substitution", "candidate_derived"] = (
        "fixture_substitution"
    )
    uploaded_mission_items: tuple[dict[str, Any], ...] = ()
    operator_approved: bool
    server_opt_in: bool
    simulation_only: Literal[True] = True
    loopback_sitl_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()
    observed_at: datetime
    receipt_hash: str
    sha256: str

    @field_validator(
        "mission_request_sequences",
        "uploaded_mission_items",
        "blocked_reasons",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_receipt(self) -> "DigitalTwinSITLMissionUploadReceipt":
        if not self.digital_twin_px4_mission_item_candidate_ref.startswith(
            "digital_twin_px4_mission_item_candidate:"
        ):
            raise DigitalTwinSITLMAVLinkUploadError(
                "Digital Twin SITL upload receipt requires mission item candidate ref"
            )
        if not self.digital_twin_sitl_process_run_ref.startswith(
            "digital_twin_sitl_process_run:"
        ):
            raise DigitalTwinSITLMAVLinkUploadError(
                "Digital Twin SITL upload receipt requires process run ref"
            )
        if not self.same_run_binding_ref:
            raise DigitalTwinSITLMAVLinkUploadError(
                "Digital Twin SITL upload receipt requires same-run binding ref"
            )
        if self.px4_mission_upload_allowed:
            if not self.operator_approved or not self.server_opt_in:
                raise DigitalTwinSITLMAVLinkUploadError(
                    "allowed Digital Twin SITL upload requires approval and opt-in"
                )
            if not self.mission_upload_attempted:
                raise DigitalTwinSITLMAVLinkUploadError(
                    "allowed Digital Twin SITL upload requires upload attempt"
                )
            if not self.mavlink_dispatch_performed:
                raise DigitalTwinSITLMAVLinkUploadError(
                    "allowed Digital Twin SITL upload requires MAVLink dispatch"
                )
        else:
            if any(
                (
                    self.mission_upload_attempted,
                    self.mavlink_dispatch_performed,
                    self.mission_upload_observed,
                )
            ):
                raise DigitalTwinSITLMAVLinkUploadError(
                    "blocked Digital Twin SITL upload cannot dispatch"
                )
            if not self.blocked_reasons:
                raise DigitalTwinSITLMAVLinkUploadError(
                    "blocked Digital Twin SITL upload requires blocked reasons"
                )
        if self.mission_upload_observed:
            if not self.mission_ack_observed or self.mission_ack_type != MAV_MISSION_ACCEPTED:
                raise DigitalTwinSITLMAVLinkUploadError(
                    "observed Digital Twin SITL upload requires accepted ACK"
                )
            if not self.mission_request_sequences:
                raise DigitalTwinSITLMAVLinkUploadError(
                    "observed Digital Twin SITL upload requires request sequences"
                )
        if self.telemetry_observed and not self.heartbeat_observed:
            raise DigitalTwinSITLMAVLinkUploadError(
                "Digital Twin SITL telemetry requires heartbeat observation"
            )
        if self.receipt_hash != self.sha256:
            raise DigitalTwinSITLMAVLinkUploadError(
                "Digital Twin SITL upload receipt hash mismatch"
            )
        return self


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _content_hash(payload: Mapping[str, Any]) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def digital_twin_sitl_mission_upload_receipt_ref(
    receipt: DigitalTwinSITLMissionUploadReceipt,
) -> str:
    return f"digital_twin_sitl_mission_upload_receipt:{receipt.receipt_id}"


def _allowed_endpoints() -> set[str]:
    endpoints = set(DEFAULT_DIGITAL_TWIN_SITL_ENDPOINTS)
    extra = os.getenv(DIGITAL_TWIN_SITL_EXTRA_ENDPOINTS_ENV, "")
    endpoints.update(item.strip() for item in extra.split(",") if item.strip())
    return endpoints


def _endpoint_host_port(endpoint: str, *, server_opt_in: bool) -> tuple[str, int]:
    if endpoint not in _allowed_endpoints():
        raise DigitalTwinSITLMAVLinkUploadError("target_endpoint_not_allowlisted")
    parsed = urlparse(endpoint)
    if parsed.scheme != "udp" or parsed.hostname != "127.0.0.1":
        raise DigitalTwinSITLMAVLinkUploadError("target_endpoint_not_loopback_udp")
    port = int(parsed.port or 0)
    if port not in {14540, 14550} and server_opt_in is not True:
        raise DigitalTwinSITLMAVLinkUploadError(
            "extra endpoint requires explicit server opt-in"
        )
    return "127.0.0.1", port


def _candidate_item_float(item: Mapping[str, Any], key: str) -> float:
    value = item.get(key)
    if not isinstance(value, int | float):
        raise DigitalTwinSITLMAVLinkUploadError(
            f"mission_item_candidate_missing_{key}"
        )
    return float(value)


def digital_twin_candidate_upload_items(
    candidate: DigitalTwinPx4MissionItemCandidate | Mapping[str, Any],
) -> tuple[PX4GazeboSITLMissionItem, ...]:
    item_candidate = (
        candidate
        if isinstance(candidate, DigitalTwinPx4MissionItemCandidate)
        else DigitalTwinPx4MissionItemCandidate.model_validate(candidate)
    )
    if item_candidate.candidate_status != "candidate_generated_for_planning_only":
        raise DigitalTwinSITLMAVLinkUploadError("mission_item_candidate_not_generated")
    if item_candidate.candidate_item_count <= 0:
        raise DigitalTwinSITLMAVLinkUploadError("mission_item_candidate_empty")
    if not item_candidate.takeoff_anchor_ref:
        raise DigitalTwinSITLMAVLinkUploadError("takeoff_anchor_missing")
    upload_items: list[PX4GazeboSITLMissionItem] = []
    for item in item_candidate.candidate_items:
        command = item.get("command")
        if command == "NAV_TAKEOFF":
            mavlink_command = MAV_CMD_NAV_TAKEOFF
            current = 1 if int(item.get("seq", 0)) == 0 else 0
        elif command == "NAV_WAYPOINT":
            mavlink_command = MAV_CMD_NAV_WAYPOINT
            current = 0
        elif command == "NAV_LAND":
            mavlink_command = MAV_CMD_NAV_LAND
            current = 0
        else:
            raise DigitalTwinSITLMAVLinkUploadError(
                "mission_item_candidate_unsupported_command"
            )
        upload_items.append(
            PX4GazeboSITLMissionItem(
                seq=int(item.get("seq", len(upload_items))),
                command=mavlink_command,
                latitude_deg=_candidate_item_float(item, "latitude_deg"),
                longitude_deg=_candidate_item_float(item, "longitude_deg"),
                altitude_m=_candidate_item_float(item, "altitude_m"),
                frame=MAV_FRAME_GLOBAL_INT,
                current=current,
            )
        )
    return tuple(upload_items)


def _observe_heartbeat(
    *,
    target_endpoint: str,
    timeout_seconds: float,
) -> bool:
    host, port = _endpoint_host_port(target_endpoint, server_opt_in=True)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout_seconds)
        sock.bind(("127.0.0.1", 0))
        sock.sendto(encode_mavlink2_heartbeat(sequence=77), (host, port))
        sock.sendto(
            encode_mavlink2_request_message(
                requested_message_id=MAVLINK_MSG_ID_HEARTBEAT,
                target_system=1,
                target_component=1,
                sequence=78,
            ),
            (host, port),
        )
        deadline = _deadline(timeout_seconds)
        while _remaining(deadline) > 0:
            sock.settimeout(_remaining(deadline))
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                return False
            try:
                decoded = decode_mavlink2_frame(data)
            except Exception:
                continue
            if decoded["msg_id"] == MAVLINK_MSG_ID_HEARTBEAT:
                return True
    return False


class DigitalTwinLoopbackMissionUploader:
    def upload(
        self,
        *,
        items: Sequence[PX4GazeboSITLMissionItem],
        target_endpoint: str,
        timeout_seconds: float,
    ) -> tuple[tuple[int, ...], int]:
        host, port = _endpoint_host_port(target_endpoint, server_opt_in=True)
        requests: list[int] = []
        sequence = 0
        deadline = _deadline(timeout_seconds)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(_remaining(deadline))
            sock.bind(("127.0.0.1", 0))
            sock.sendto(
                encode_mavlink2_mission_count(
                    count=len(items),
                    target_system=1,
                    target_component=1,
                    sequence=sequence,
                ),
                (host, port),
            )
            sequence += 1
            while len(requests) < len(items):
                sock.settimeout(_remaining(deadline))
                data, _addr = sock.recvfrom(2048)
                requested_seq = decode_mavlink2_mission_request_int(data)
                if requested_seq is None:
                    continue
                if requested_seq >= len(items):
                    raise DigitalTwinSITLMAVLinkUploadError(
                        "mission_request_seq_out_of_range"
                    )
                requests.append(requested_seq)
                sock.sendto(
                    encode_mavlink2_mission_item_int(
                        item=items[requested_seq],
                        target_system=1,
                        target_component=1,
                        sequence=sequence,
                    ),
                    (host, port),
                )
                sequence += 1
            while True:
                sock.settimeout(_remaining(deadline))
                data, _addr = sock.recvfrom(2048)
                ack_type = decode_mavlink2_mission_ack_type(data)
                if ack_type is not None:
                    return tuple(requests), ack_type


def _deadline(timeout_seconds: float) -> float:
    import time

    return time.monotonic() + max(0.01, float(timeout_seconds))


def _remaining(deadline: float) -> float:
    import time

    return max(0.01, deadline - time.monotonic())


def build_digital_twin_sitl_mission_upload_receipt(
    *,
    px4_mission_item_candidate: DigitalTwinPx4MissionItemCandidate | Mapping[str, Any],
    sitl_process_run: DigitalTwinSITLProcessRun | Mapping[str, Any],
    target_endpoint: str = DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
    operator_approved: bool = False,
    server_opt_in: bool = False,
    same_run_binding_ref: str,
    uploader: DigitalTwinSITLMissionUploader | None = None,
    timeout_seconds: float = 5.0,
    now: datetime | None = None,
) -> DigitalTwinSITLMissionUploadReceipt:
    candidate = (
        px4_mission_item_candidate
        if isinstance(px4_mission_item_candidate, DigitalTwinPx4MissionItemCandidate)
        else DigitalTwinPx4MissionItemCandidate.model_validate(
            px4_mission_item_candidate
        )
    )
    process_run = (
        sitl_process_run
        if isinstance(sitl_process_run, DigitalTwinSITLProcessRun)
        else DigitalTwinSITLProcessRun.model_validate(sitl_process_run)
    )
    observed_at = _utc(now)
    candidate_ref = digital_twin_px4_mission_item_candidate_ref(candidate)
    process_ref = digital_twin_sitl_process_run_ref(process_run)
    blocked: list[str] = []
    upload_items: tuple[PX4GazeboSITLMissionItem, ...] = ()
    try:
        _endpoint_host_port(target_endpoint, server_opt_in=server_opt_in)
    except DigitalTwinSITLMAVLinkUploadError as exc:
        blocked.append(str(exc))
    if server_opt_in is not True:
        blocked.append("server_opt_in_missing")
    if operator_approved is not True:
        blocked.append("operator_approval_missing")
    if not process_run.gazebo_execution_invoked:
        blocked.append("digital_twin_world_process_not_invoked")
    if process_run.startup_error_observed:
        blocked.append("digital_twin_world_startup_error_observed")
    try:
        upload_items = digital_twin_candidate_upload_items(candidate)
    except DigitalTwinSITLMAVLinkUploadError as exc:
        blocked.append(str(exc))

    request_sequences: tuple[int, ...] = ()
    ack_type: int | None = None
    heartbeat_observed = False
    if not blocked:
        try:
            resolved_uploader = uploader or DigitalTwinLoopbackMissionUploader()
            request_sequences, ack_type = resolved_uploader.upload(
                items=upload_items,
                target_endpoint=target_endpoint,
                timeout_seconds=timeout_seconds,
            )
            heartbeat_observed = bool(
                getattr(resolved_uploader, "heartbeat_observed", False)
            ) or _observe_heartbeat(
                target_endpoint=target_endpoint,
                timeout_seconds=max(0.5, min(timeout_seconds, 3.0)),
            )
        except (socket.timeout, OSError, Exception) as exc:
            blocked.append(f"mission_upload_failed:{type(exc).__name__}")

    uploaded = not blocked and ack_type == MAV_MISSION_ACCEPTED
    if not uploaded and ack_type is not None:
        blocked.append(f"mission_ack_type_{ack_type}")
    payload = {
        "schema_version": DIGITAL_TWIN_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION,
        "digital_twin_px4_mission_item_candidate_ref": candidate_ref,
        "digital_twin_sitl_process_run_ref": process_ref,
        "target_endpoint": target_endpoint,
        "mission_upload_attempted": not blocked or ack_type is not None,
        "mission_upload_observed": uploaded,
        "mission_ack_type": ack_type,
        "mission_request_sequences": request_sequences,
        "same_run_binding_ref": same_run_binding_ref,
        "mission_items_source": "candidate_derived",
        "uploaded_mission_items": tuple(
            item.model_dump(mode="json") for item in upload_items
        ),
        "blocked_reasons": tuple(sorted(set(blocked))),
    }
    digest = _content_hash(payload)
    return DigitalTwinSITLMissionUploadReceipt(
        receipt_id="digital_twin_sitl_mission_upload_receipt_" + digest[:12],
        digital_twin_px4_mission_item_candidate_ref=candidate_ref,
        digital_twin_sitl_process_run_ref=process_ref,
        target_endpoint=target_endpoint,
        mission_upload_attempted=not blocked or ack_type is not None,
        px4_mission_upload_allowed=uploaded,
        mavlink_dispatch_performed=uploaded,
        mission_upload_observed=uploaded,
        mission_ack_observed=ack_type is not None,
        mission_ack_type=ack_type,
        mission_request_sequences=request_sequences,
        telemetry_observed=heartbeat_observed,
        heartbeat_observed=heartbeat_observed,
        position_observed=False,
        same_run_binding_ref=same_run_binding_ref,
        candidate_item_count=candidate.candidate_item_count,
        mission_items_source="candidate_derived",
        uploaded_mission_items=tuple(
            item.model_dump(mode="json") for item in upload_items
        ),
        operator_approved=operator_approved,
        server_opt_in=server_opt_in,
        blocked_reasons=tuple(sorted(set(blocked))),
        observed_at=observed_at,
        receipt_hash=digest,
        sha256=digest,
    )


__all__ = [
    "DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT",
    "DIGITAL_TWIN_SITL_MISSION_UPLOAD_OPT_IN_ENV",
    "DIGITAL_TWIN_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION",
    "DigitalTwinSITLMAVLinkUploadError",
    "DigitalTwinSITLMissionUploadReceipt",
    "build_digital_twin_sitl_mission_upload_receipt",
    "digital_twin_candidate_upload_items",
    "digital_twin_sitl_mission_upload_receipt_ref",
]
