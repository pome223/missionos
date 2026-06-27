"""PX4/Gazebo SITL-only MAVLink mission upload.

This module is the first Mission OS boundary that permits external dispatch for
this epic, and it is restricted to a hard-coded loopback PX4 SITL endpoint. It
uploads a bounded mission to SITL only; it never permits hardware targets,
physical execution, ROS actions, actuator execution, or Gazebo entity mutation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import importlib
import json
import math
import os
import socket
import struct
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.delivery_mission_contract import DeliveryMissionContract
from src.runtime.simulated_delivery_command import (
    SimulatedCommandApproval,
    SimulatedCommandProposal,
    SimulatorCommandExecutionPreflight,
    SimulatorCommandExecutionPreflightStatus,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.px4_real_mavlink_transport import (
    MAVLINK_MSG_ID_MISSION_ACK,
    MAVLINK_MSG_ID_MISSION_COUNT,
    MAVLINK_MSG_ID_MISSION_ITEM_INT,
    MAVLINK_MSG_ID_MISSION_REQUEST_INT,
    decode_mavlink2_frame,
    encode_mavlink2_frame,
)

PX4_GAZEBO_SITL_MISSION_ITEM_SCHEMA_VERSION = "px4_gazebo_sitl_mission_item.v1"
PX4_GAZEBO_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION = (
    "px4_gazebo_sitl_mission_upload_receipt.v1"
)

PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT = "udp://127.0.0.1:14540"
PX4_GAZEBO_SITL_MISSION_UPLOAD_HOST = "127.0.0.1"
PX4_GAZEBO_SITL_MISSION_UPLOAD_PORT = 14540
PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_OPT_IN_ENV = (
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER"
)
PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_CONTAINER_ENV = (
    "MISSION_DESIGNER_PX4_GAZEBO_SITL_DOCKER_CONTAINER"
)
PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_REUSE_CONTAINER_ENV = (
    "MISSION_DESIGNER_PX4_GAZEBO_SITL_DOCKER_REUSE_CONTAINER"
)

MAV_FRAME_GLOBAL_INT = 5
MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6
MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_NAV_LOITER_TIME = 19
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_LAND = 21
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_MISSION_ACCEPTED = 0
MAV_MISSION_TYPE_MISSION = 0
SITL_MISSION_UPLOAD_ABSOLUTE_MAX_ALTITUDE_M = 120.0
SITL_MISSION_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M = 500.0
SITL_MISSION_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M = 20_000.0


class PX4GazeboSITLMissionUploadError(RuntimeError):
    """Raised when SITL mission upload cannot proceed safely."""


class PX4GazeboSITLMissionUploadStatus(str, Enum):
    UPLOADED = "uploaded"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


class PX4GazeboSITLMissionItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SITL_MISSION_ITEM_SCHEMA_VERSION] = (
        PX4_GAZEBO_SITL_MISSION_ITEM_SCHEMA_VERSION
    )
    seq: int = Field(ge=0)
    command: Literal[16, 19, 20, 21, 22]
    frame: Literal[MAV_FRAME_GLOBAL_INT, MAV_FRAME_GLOBAL_RELATIVE_ALT_INT] = (
        MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
    )
    param1: float = 0.0
    param2: float = 0.0
    param3: float = 0.0
    param4: float = 0.0
    latitude_deg: float = Field(ge=-90, le=90)
    longitude_deg: float = Field(ge=-180, le=180)
    # Digital Twin mission candidates may carry source-derived mountain AMSL
    # altitudes up to vehicle_flight_envelope.max_takeoff_altitude_m.
    # Safe-Route SITL is separately bounded by its own route builder.
    # TODO(#563): add an explicit test that Safe-Route route_builder rejects
    # altitude_m > 500 even though this shared model accepts up to 5000.
    altitude_m: float = Field(ge=0, le=5000)
    current: int = Field(default=0, ge=0, le=1)
    autocontinue: Literal[1] = 1


class PX4GazeboSITLMissionUploadReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[PX4_GAZEBO_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION] = (
        PX4_GAZEBO_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str
    simulator_command_execution_preflight_ref: str
    simulated_command_proposal_ref: str
    simulated_command_approval_ref: str
    delivery_mission_contract_ref: str
    target_endpoint: Literal[PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT]
    target_host: Literal[PX4_GAZEBO_SITL_MISSION_UPLOAD_HOST]
    target_port: Literal[PX4_GAZEBO_SITL_MISSION_UPLOAD_PORT]
    upload_status: PX4GazeboSITLMissionUploadStatus
    blocked_reasons: tuple[str, ...] = ()
    mission_items: tuple[PX4GazeboSITLMissionItem, ...]
    mission_item_count: int = Field(ge=0)
    mission_request_sequences: tuple[int, ...] = ()
    mission_ack_type: int | None = None
    mission_ack_observed: bool = False
    max_altitude_m: float
    max_mission_items: int
    geofence_radius_m: float
    external_dispatch_performed: bool
    gazebo_simulator_command_performed: bool
    mavlink_dispatch_performed: bool
    px4_mission_upload_performed: bool
    mission_upload_target_whitelisted: bool
    sitl_endpoint_whitelist_literal: Literal[
        PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT
    ] = PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT
    hardware_target_allowed: Literal[False] = False
    gazebo_entity_mutation_performed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    ros_dispatch_performed: Literal[False] = False
    actuator_execution_performed: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    uploaded_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("uploaded_at", mode="before")
    @classmethod
    def _coerce_uploaded_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @field_validator("mission_items", "mission_request_sequences", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @model_validator(mode="after")
    def _validate_receipt(self) -> "PX4GazeboSITLMissionUploadReceipt":
        if self.mission_item_count != len(self.mission_items):
            raise PX4GazeboSITLMissionUploadError("mission item count mismatch")
        if self.upload_status is PX4GazeboSITLMissionUploadStatus.UPLOADED:
            if self.blocked_reasons:
                raise PX4GazeboSITLMissionUploadError(
                    "uploaded receipt cannot be blocked"
                )
            if self.external_dispatch_performed is not True:
                raise PX4GazeboSITLMissionUploadError(
                    "uploaded receipt requires external dispatch"
                )
            if self.mavlink_dispatch_performed is not True:
                raise PX4GazeboSITLMissionUploadError(
                    "uploaded receipt requires MAVLink dispatch"
                )
            if self.px4_mission_upload_performed is not True:
                raise PX4GazeboSITLMissionUploadError(
                    "uploaded receipt requires mission upload"
                )
            if self.mission_ack_observed is not True or self.mission_ack_type != 0:
                raise PX4GazeboSITLMissionUploadError(
                    "uploaded receipt requires accepted MISSION_ACK"
                )
        else:
            if not self.blocked_reasons:
                raise PX4GazeboSITLMissionUploadError(
                    "blocked/timeout receipt requires reasons"
                )
            if any(
                (
                    self.external_dispatch_performed,
                    self.mavlink_dispatch_performed,
                    self.px4_mission_upload_performed,
                )
            ):
                raise PX4GazeboSITLMissionUploadError("blocked upload cannot dispatch")
        if self.hardware_target_allowed or self.physical_execution_invoked:
            raise PX4GazeboSITLMissionUploadError(
                "SITL mission upload forbids hardware/physical execution"
            )
        return self


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


def _as_tuple(values: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(sorted({str(item) for item in (values or ()) if str(item)}))


def _bounded_max_altitude(requested_max_altitude_m: float) -> float:
    return min(
        float(requested_max_altitude_m),
        SITL_MISSION_UPLOAD_ABSOLUTE_MAX_ALTITUDE_M,
    )


def _bounded_geofence_radius(requested_geofence_radius_m: float) -> float:
    return min(
        float(requested_geofence_radius_m),
        SITL_MISSION_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M,
    )


def _contract_ref(contract: DeliveryMissionContract) -> str:
    return f"delivery_mission_contract:{contract.contract_id}"


def _preflight_ref(preflight: SimulatorCommandExecutionPreflight) -> str:
    return f"simulator_command_execution_preflight:{preflight.preflight_id}"


def _proposal_ref(proposal: SimulatedCommandProposal) -> str:
    return f"simulated_command_proposal:{proposal.proposal_id}"


def _approval_ref(approval: SimulatedCommandApproval) -> str:
    return f"simulated_command_approval:{approval.approval_id}"


def _to_contract(
    value: DeliveryMissionContract | Mapping[str, Any],
) -> DeliveryMissionContract:
    if isinstance(value, DeliveryMissionContract):
        return value
    return DeliveryMissionContract.model_validate(dict(value))


def _to_preflight(
    value: SimulatorCommandExecutionPreflight | Mapping[str, Any],
) -> SimulatorCommandExecutionPreflight:
    if isinstance(value, SimulatorCommandExecutionPreflight):
        return value
    return SimulatorCommandExecutionPreflight.model_validate(dict(value))


def _to_proposal(
    value: SimulatedCommandProposal | Mapping[str, Any],
) -> SimulatedCommandProposal:
    if isinstance(value, SimulatedCommandProposal):
        return value
    return SimulatedCommandProposal.model_validate(dict(value))


def _to_approval(
    value: SimulatedCommandApproval | Mapping[str, Any],
) -> SimulatedCommandApproval:
    if isinstance(value, SimulatedCommandApproval):
        return value
    return SimulatedCommandApproval.model_validate(dict(value))


def validate_sitl_mission_upload_target(
    *, target_endpoint: str, allow_sitl_mission_upload: bool
) -> None:
    if allow_sitl_mission_upload is not True:
        raise PX4GazeboSITLMissionUploadError(
            "SITL mission upload requires explicit opt-in"
        )
    if target_endpoint != PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT:
        raise PX4GazeboSITLMissionUploadError("target_not_in_simulator_whitelist")


def _distance_m(
    *, origin_lat: float, origin_lon: float, target_lat: float, target_lon: float
) -> float:
    radius_m = 6_371_000.0
    origin_phi = math.radians(origin_lat)
    target_phi = math.radians(target_lat)
    delta_phi = math.radians(target_lat - origin_lat)
    delta_lambda = math.radians(target_lon - origin_lon)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(origin_phi)
        * math.cos(target_phi)
        * math.sin(delta_lambda / 2.0) ** 2
    )
    return radius_m * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def validate_sitl_mission_items_within_geofence(
    contract: DeliveryMissionContract | Mapping[str, Any],
    items: Sequence[PX4GazeboSITLMissionItem],
    *,
    geofence_radius_m: float,
) -> None:
    resolved = _to_contract(contract)
    pickup = resolved.pickup_location
    for item in items:
        distance_m = _distance_m(
            origin_lat=pickup.latitude,
            origin_lon=pickup.longitude,
            target_lat=item.latitude_deg,
            target_lon=item.longitude_deg,
        )
        if distance_m > geofence_radius_m:
            raise PX4GazeboSITLMissionUploadError(
                f"mission_item_{item.seq}_outside_sitl_geofence"
            )


def build_sitl_mission_items_from_contract(
    contract: DeliveryMissionContract | Mapping[str, Any],
    *,
    max_altitude_m: float = 120.0,
    max_mission_items: int = 8,
) -> tuple[PX4GazeboSITLMissionItem, ...]:
    resolved = _to_contract(contract)
    effective_max_altitude_m = _bounded_max_altitude(max_altitude_m)
    pickup = resolved.pickup_location
    dropoff = resolved.dropoff_location
    dropoff_altitude = float(dropoff.altitude_m or 30.0)
    staged_altitude = min(
        max(20.0, dropoff_altitude / 2.0),
        effective_max_altitude_m,
    )
    final_altitude = min(
        max(dropoff_altitude, staged_altitude),
        effective_max_altitude_m,
    )
    midpoint_lat = (pickup.latitude + dropoff.latitude) / 2.0
    midpoint_lon = (pickup.longitude + dropoff.longitude) / 2.0
    items = (
        PX4GazeboSITLMissionItem(
            seq=0,
            command=MAV_CMD_NAV_TAKEOFF,
            latitude_deg=pickup.latitude,
            longitude_deg=pickup.longitude,
            altitude_m=min(15.0, effective_max_altitude_m),
            current=1,
        ),
        PX4GazeboSITLMissionItem(
            seq=1,
            command=MAV_CMD_NAV_WAYPOINT,
            latitude_deg=midpoint_lat,
            longitude_deg=midpoint_lon,
            altitude_m=staged_altitude,
        ),
        PX4GazeboSITLMissionItem(
            seq=2,
            command=MAV_CMD_NAV_WAYPOINT,
            latitude_deg=dropoff.latitude,
            longitude_deg=dropoff.longitude,
            altitude_m=final_altitude,
        ),
        PX4GazeboSITLMissionItem(
            seq=3,
            command=MAV_CMD_NAV_LAND,
            latitude_deg=dropoff.latitude,
            longitude_deg=dropoff.longitude,
            altitude_m=0.0,
        ),
    )
    if len(items) > max_mission_items:
        raise PX4GazeboSITLMissionUploadError("mission_item_count_exceeds_limit")
    if any(item.altitude_m > effective_max_altitude_m for item in items):
        raise PX4GazeboSITLMissionUploadError("mission_item_altitude_exceeds_limit")
    return items


def coerce_sitl_mission_items(
    items: Sequence[PX4GazeboSITLMissionItem | Mapping[str, Any]],
    *,
    max_altitude_m: float = 120.0,
    max_mission_items: int = 8,
) -> tuple[PX4GazeboSITLMissionItem, ...]:
    effective_max_altitude_m = _bounded_max_altitude(max_altitude_m)
    resolved = tuple(
        (
            item
            if isinstance(item, PX4GazeboSITLMissionItem)
            else PX4GazeboSITLMissionItem.model_validate(dict(item))
        )
        for item in items
    )
    if len(resolved) > max_mission_items:
        raise PX4GazeboSITLMissionUploadError("mission_item_count_exceeds_limit")
    if any(item.altitude_m > effective_max_altitude_m for item in resolved):
        raise PX4GazeboSITLMissionUploadError("mission_item_altitude_exceeds_limit")
    if tuple(item.seq for item in resolved) != tuple(range(len(resolved))):
        raise PX4GazeboSITLMissionUploadError("mission_item_sequence_not_contiguous")
    return resolved


def encode_mavlink2_mission_count(
    *, count: int, target_system: int, target_component: int, sequence: int
) -> bytes:
    payload = struct.pack(
        "<HBBB", count, target_system, target_component, MAV_MISSION_TYPE_MISSION
    )
    return encode_mavlink2_frame(
        msg_id=MAVLINK_MSG_ID_MISSION_COUNT,
        payload=payload,
        sequence=sequence,
        system_id=255,
        component_id=190,
    )


def encode_mavlink2_mission_item_int(
    *,
    item: PX4GazeboSITLMissionItem,
    target_system: int,
    target_component: int,
    sequence: int,
) -> bytes:
    payload = struct.pack(
        "<ffffiifHHBBBBBB",
        float(item.param1),
        float(item.param2),
        float(item.param3),
        float(item.param4),
        int(round(item.latitude_deg * 10_000_000)),
        int(round(item.longitude_deg * 10_000_000)),
        float(item.altitude_m),
        item.seq,
        item.command,
        target_system,
        target_component,
        item.frame,
        item.current,
        item.autocontinue,
        MAV_MISSION_TYPE_MISSION,
    )
    return encode_mavlink2_frame(
        msg_id=MAVLINK_MSG_ID_MISSION_ITEM_INT,
        payload=payload,
        sequence=sequence,
        system_id=255,
        component_id=190,
    )


def decode_mavlink2_mission_request_int(frame: bytes) -> int | None:
    decoded = decode_mavlink2_frame(frame)
    if decoded["msg_id"] != MAVLINK_MSG_ID_MISSION_REQUEST_INT:
        return None
    if len(decoded["payload"]) < 2:
        return None
    return int(struct.unpack("<H", decoded["payload"][:2])[0])


def decode_mavlink2_mission_ack_type(frame: bytes) -> int | None:
    decoded = decode_mavlink2_frame(frame)
    if decoded["msg_id"] != MAVLINK_MSG_ID_MISSION_ACK:
        return None
    if len(decoded["payload"]) < 3:
        return None
    return int(decoded["payload"][2])


class PX4GazeboSITLMissionUploader:
    def upload(
        self,
        *,
        items: Sequence[PX4GazeboSITLMissionItem],
        target_endpoint: str,
        timeout_seconds: float,
    ) -> tuple[tuple[int, ...], int]:
        docker_exec_result = _upload_via_docker_exec_if_enabled(
            items=items,
            target_endpoint=target_endpoint,
            timeout_seconds=timeout_seconds,
        )
        if docker_exec_result is not None:
            return docker_exec_result

        host, port = _endpoint_host_port(target_endpoint)
        requests: list[int] = []
        sequence = 0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_seconds)
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
                data, _addr = sock.recvfrom(2048)
                requested_seq = decode_mavlink2_mission_request_int(data)
                if requested_seq is None:
                    continue
                if requested_seq >= len(items):
                    raise PX4GazeboSITLMissionUploadError(
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
                data, _addr = sock.recvfrom(2048)
                ack_type = decode_mavlink2_mission_ack_type(data)
                if ack_type is not None:
                    return tuple(requests), ack_type


def _upload_via_docker_exec_if_enabled(
    *,
    items: Sequence[PX4GazeboSITLMissionItem],
    target_endpoint: str,
    timeout_seconds: float,
) -> tuple[tuple[int, ...], int] | None:
    if os.getenv(PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_OPT_IN_ENV) != "1":
        return None
    if target_endpoint != PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT:
        raise PX4GazeboSITLMissionUploadError("target_not_in_simulator_whitelist")
    if timeout_seconds <= 0:
        raise PX4GazeboSITLMissionUploadError("timeout_seconds must be positive")

    try:
        sitl_upload_smoke = importlib.import_module(
            "scripts.smoke_px4_gazebo_sitl_mission_upload"
        )
    except Exception as exc:  # pragma: no cover - import failure is environment-specific.
        raise PX4GazeboSITLMissionUploadError(
            "docker_exec_uploader_unavailable"
        ) from exc

    container_name = os.getenv(PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_CONTAINER_ENV)
    previous_container_name = getattr(sitl_upload_smoke, "CONTAINER_NAME", None)
    if container_name:
        sitl_upload_smoke.CONTAINER_NAME = container_name
    reuse_container = (
        os.getenv(PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_REUSE_CONTAINER_ENV) == "1"
        and bool(container_name)
    )
    started_container = False
    try:
        if not reuse_container:
            sitl_upload_smoke._start_container()
            started_container = True
        observed = sitl_upload_smoke._actual_upload(items=items)
    except Exception as exc:
        raise PX4GazeboSITLMissionUploadError("docker_exec_upload_failed") from exc
    finally:
        if started_container:
            try:
                sitl_upload_smoke._stop_container()
            except Exception:
                pass
        if previous_container_name is not None:
            sitl_upload_smoke.CONTAINER_NAME = previous_container_name

    observed_items = tuple(tuple(item) for item in (observed.get("mission_items") or ()))
    expected_items = sitl_upload_smoke._mission_upload_item_tuples(items)
    if observed_items != expected_items:
        raise PX4GazeboSITLMissionUploadError(
            "docker_exec_upload_item_binding_mismatch"
        )
    request_sequences = tuple(
        int(item) for item in (observed.get("mission_request_sequences") or ())
    )
    ack_type = observed.get("mission_ack_type")
    if observed.get("mission_ack_observed") is not True or ack_type is None:
        raise PX4GazeboSITLMissionUploadError("docker_exec_upload_ack_missing")
    return request_sequences, int(ack_type)


def _endpoint_host_port(endpoint: str) -> tuple[str, int]:
    if endpoint != PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT:
        raise PX4GazeboSITLMissionUploadError("target_not_in_simulator_whitelist")
    return PX4_GAZEBO_SITL_MISSION_UPLOAD_HOST, PX4_GAZEBO_SITL_MISSION_UPLOAD_PORT


def build_px4_gazebo_sitl_mission_upload_receipt(
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulator_command_execution_preflight: (
        SimulatorCommandExecutionPreflight | Mapping[str, Any]
    ),
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    target_endpoint: str,
    allow_sitl_mission_upload: bool,
    uploader: PX4GazeboSITLMissionUploader | None = None,
    timeout_seconds: float = 5.0,
    max_altitude_m: float = 120.0,
    max_mission_items: int = 8,
    geofence_radius_m: float = SITL_MISSION_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M,
    mission_items_override: (
        Sequence[PX4GazeboSITLMissionItem | Mapping[str, Any]] | None
    ) = None,
    now: datetime | None = None,
) -> PX4GazeboSITLMissionUploadReceipt:
    contract = _to_contract(delivery_mission_contract)
    preflight = _to_preflight(simulator_command_execution_preflight)
    proposal = _to_proposal(simulated_command_proposal)
    approval = _to_approval(simulated_command_approval)
    uploaded_at = _utc(now)
    effective_max_altitude_m = _bounded_max_altitude(max_altitude_m)
    effective_geofence_radius_m = _bounded_geofence_radius(geofence_radius_m)
    contract_ref = _contract_ref(contract)
    preflight_ref = _preflight_ref(preflight)
    proposal_ref = _proposal_ref(proposal)
    approval_ref = _approval_ref(approval)
    blocked: list[str] = []
    try:
        validate_sitl_mission_upload_target(
            target_endpoint=target_endpoint,
            allow_sitl_mission_upload=allow_sitl_mission_upload,
        )
    except PX4GazeboSITLMissionUploadError as exc:
        blocked.append(str(exc))
    if (
        preflight.status
        is not SimulatorCommandExecutionPreflightStatus.READY_FOR_SIMULATOR_COMMAND
    ):
        blocked.append("simulator_command_preflight_not_ready")
    if preflight.simulated_command_proposal_ref != proposal_ref:
        blocked.append("preflight_proposal_ref_mismatch")
    if preflight.simulated_command_approval_ref != approval_ref:
        blocked.append("preflight_approval_ref_mismatch")
    if proposal.delivery_mission_contract_ref != contract_ref:
        blocked.append("proposal_contract_ref_mismatch")
    if approval.simulated_command_proposal_ref != proposal_ref:
        blocked.append("approval_proposal_ref_mismatch")
    items: tuple[PX4GazeboSITLMissionItem, ...] = ()
    if not blocked:
        try:
            items = (
                coerce_sitl_mission_items(
                    mission_items_override,
                    max_altitude_m=effective_max_altitude_m,
                    max_mission_items=max_mission_items,
                )
                if mission_items_override is not None
                else build_sitl_mission_items_from_contract(
                    contract,
                    max_altitude_m=effective_max_altitude_m,
                    max_mission_items=max_mission_items,
                )
            )
            validate_sitl_mission_items_within_geofence(
                contract,
                items,
                geofence_radius_m=effective_geofence_radius_m,
            )
        except PX4GazeboSITLMissionUploadError as exc:
            blocked.append(str(exc))
    if blocked:
        return _mission_upload_receipt(
            contract_ref=contract_ref,
            preflight_ref=preflight_ref,
            proposal_ref=proposal_ref,
            approval_ref=approval_ref,
            target_endpoint=PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
            status=PX4GazeboSITLMissionUploadStatus.BLOCKED,
            blocked_reasons=_as_tuple(blocked),
            items=items,
            request_sequences=(),
            ack_type=None,
            max_altitude_m=effective_max_altitude_m,
            max_mission_items=max_mission_items,
            geofence_radius_m=effective_geofence_radius_m,
            uploaded_at=uploaded_at,
            timeout_seconds=timeout_seconds,
        )
    try:
        request_sequences, ack_type = (
            uploader or PX4GazeboSITLMissionUploader()
        ).upload(
            items=items,
            target_endpoint=target_endpoint,
            timeout_seconds=timeout_seconds,
        )
    except (PX4GazeboSITLMissionUploadError, socket.timeout) as exc:
        return _mission_upload_receipt(
            contract_ref=contract_ref,
            preflight_ref=preflight_ref,
            proposal_ref=proposal_ref,
            approval_ref=approval_ref,
            target_endpoint=target_endpoint,
            status=PX4GazeboSITLMissionUploadStatus.TIMEOUT,
            blocked_reasons=(
                (
                    "mission_upload_timeout"
                    if isinstance(exc, socket.timeout)
                    else str(exc)
                ),
            ),
            items=items,
            request_sequences=(),
            ack_type=None,
            max_altitude_m=effective_max_altitude_m,
            max_mission_items=max_mission_items,
            geofence_radius_m=effective_geofence_radius_m,
            uploaded_at=uploaded_at,
            timeout_seconds=timeout_seconds,
        )
    if ack_type != MAV_MISSION_ACCEPTED:
        return _mission_upload_receipt(
            contract_ref=contract_ref,
            preflight_ref=preflight_ref,
            proposal_ref=proposal_ref,
            approval_ref=approval_ref,
            target_endpoint=target_endpoint,
            status=PX4GazeboSITLMissionUploadStatus.BLOCKED,
            blocked_reasons=(f"mission_ack_type_{ack_type}",),
            items=items,
            request_sequences=request_sequences,
            ack_type=ack_type,
            max_altitude_m=effective_max_altitude_m,
            max_mission_items=max_mission_items,
            geofence_radius_m=effective_geofence_radius_m,
            uploaded_at=uploaded_at,
            timeout_seconds=timeout_seconds,
        )
    return _mission_upload_receipt(
        contract_ref=contract_ref,
        preflight_ref=preflight_ref,
        proposal_ref=proposal_ref,
        approval_ref=approval_ref,
        target_endpoint=target_endpoint,
        status=PX4GazeboSITLMissionUploadStatus.UPLOADED,
        blocked_reasons=(),
        items=items,
        request_sequences=request_sequences,
        ack_type=ack_type,
        max_altitude_m=effective_max_altitude_m,
        max_mission_items=max_mission_items,
        geofence_radius_m=effective_geofence_radius_m,
        uploaded_at=uploaded_at,
        timeout_seconds=timeout_seconds,
    )


def _mission_upload_receipt(
    *,
    contract_ref: str,
    preflight_ref: str,
    proposal_ref: str,
    approval_ref: str,
    target_endpoint: str,
    status: PX4GazeboSITLMissionUploadStatus,
    blocked_reasons: tuple[str, ...],
    items: tuple[PX4GazeboSITLMissionItem, ...],
    request_sequences: tuple[int, ...],
    ack_type: int | None,
    max_altitude_m: float,
    max_mission_items: int,
    geofence_radius_m: float,
    uploaded_at: datetime,
    timeout_seconds: float | None = None,
) -> PX4GazeboSITLMissionUploadReceipt:
    uploaded = status is PX4GazeboSITLMissionUploadStatus.UPLOADED
    payload = {
        "contract_ref": contract_ref,
        "preflight_ref": preflight_ref,
        "proposal_ref": proposal_ref,
        "approval_ref": approval_ref,
        "target_endpoint": target_endpoint,
        "status": status.value,
        "item_count": len(items),
        "request_sequences": request_sequences,
        "ack_type": ack_type,
        "timeout_seconds": timeout_seconds,
    }
    return PX4GazeboSITLMissionUploadReceipt(
        receipt_id=_stable_id("px4_gazebo_sitl_mission_upload_receipt", payload),
        simulator_command_execution_preflight_ref=preflight_ref,
        simulated_command_proposal_ref=proposal_ref,
        simulated_command_approval_ref=approval_ref,
        delivery_mission_contract_ref=contract_ref,
        target_endpoint=PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
        target_host=PX4_GAZEBO_SITL_MISSION_UPLOAD_HOST,
        target_port=PX4_GAZEBO_SITL_MISSION_UPLOAD_PORT,
        upload_status=status,
        blocked_reasons=blocked_reasons,
        mission_items=items,
        mission_item_count=len(items),
        mission_request_sequences=request_sequences,
        mission_ack_type=ack_type,
        mission_ack_observed=ack_type is not None,
        max_altitude_m=max_altitude_m,
        max_mission_items=max_mission_items,
        geofence_radius_m=geofence_radius_m,
        external_dispatch_performed=uploaded,
        gazebo_simulator_command_performed=uploaded,
        mavlink_dispatch_performed=uploaded,
        px4_mission_upload_performed=uploaded,
        mission_upload_target_whitelisted=target_endpoint
        == PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
        uploaded_at=uploaded_at,
        metadata={
            "sitl_whitelist_hardcoded": True,
            "config_override_allowed": False,
            "issue": 410,
            "timeout_seconds": timeout_seconds,
        },
    )


def attach_px4_gazebo_sitl_mission_upload_receipt(
    task_id: str,
    *,
    delivery_mission_contract: DeliveryMissionContract | Mapping[str, Any],
    simulator_command_execution_preflight: (
        SimulatorCommandExecutionPreflight | Mapping[str, Any]
    ),
    simulated_command_proposal: SimulatedCommandProposal | Mapping[str, Any],
    simulated_command_approval: SimulatedCommandApproval | Mapping[str, Any],
    target_endpoint: str,
    allow_sitl_mission_upload: bool,
    uploader: PX4GazeboSITLMissionUploader | None = None,
    timeout_seconds: float = 5.0,
    max_altitude_m: float = 120.0,
    max_mission_items: int = 8,
    geofence_radius_m: float = SITL_MISSION_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M,
    mission_items_override: (
        Sequence[PX4GazeboSITLMissionItem | Mapping[str, Any]] | None
    ) = None,
    now: datetime | None = None,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    store = (task_store_factory or get_task_store)()
    current = store.get(task_id)
    if current is None:
        raise PX4GazeboSITLMissionUploadError(
            f"task {task_id} not found; cannot attach SITL mission upload receipt"
        )
    receipt = build_px4_gazebo_sitl_mission_upload_receipt(
        delivery_mission_contract=delivery_mission_contract,
        simulator_command_execution_preflight=simulator_command_execution_preflight,
        simulated_command_proposal=simulated_command_proposal,
        simulated_command_approval=simulated_command_approval,
        target_endpoint=target_endpoint,
        allow_sitl_mission_upload=allow_sitl_mission_upload,
        uploader=uploader,
        timeout_seconds=timeout_seconds,
        max_altitude_m=max_altitude_m,
        max_mission_items=max_mission_items,
        geofence_radius_m=geofence_radius_m,
        mission_items_override=mission_items_override,
        now=now,
    )
    artifacts = {
        "px4_gazebo_sitl_mission_upload_receipt": receipt.model_dump(mode="json")
    }
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise PX4GazeboSITLMissionUploadError(
            f"task {task_id} disappeared while attaching SITL mission upload receipt"
        )
    return {**artifacts, "task": updated}


__all__ = [
    "PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT",
    "PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_CONTAINER_ENV",
    "PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_OPT_IN_ENV",
    "PX4_GAZEBO_SITL_DOCKER_EXEC_UPLOADER_REUSE_CONTAINER_ENV",
    "PX4_GAZEBO_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION",
    "PX4_GAZEBO_SITL_MISSION_ITEM_SCHEMA_VERSION",
    "SITL_MISSION_UPLOAD_ABSOLUTE_GEOFENCE_RADIUS_M",
    "SITL_MISSION_UPLOAD_ABSOLUTE_MAX_ALTITUDE_M",
    "SITL_MISSION_UPLOAD_DEFAULT_GEOFENCE_RADIUS_M",
    "MAV_CMD_NAV_LOITER_TIME",
    "PX4GazeboSITLMissionItem",
    "PX4GazeboSITLMissionUploadError",
    "PX4GazeboSITLMissionUploadReceipt",
    "PX4GazeboSITLMissionUploadStatus",
    "PX4GazeboSITLMissionUploader",
    "attach_px4_gazebo_sitl_mission_upload_receipt",
    "build_px4_gazebo_sitl_mission_upload_receipt",
    "build_sitl_mission_items_from_contract",
    "coerce_sitl_mission_items",
    "decode_mavlink2_mission_ack_type",
    "decode_mavlink2_mission_request_int",
    "encode_mavlink2_mission_count",
    "encode_mavlink2_mission_item_int",
    "validate_sitl_mission_items_within_geofence",
    "validate_sitl_mission_upload_target",
]
