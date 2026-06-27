"""Observed ARM/AUTO/takeoff receipt for Digital Twin SITL-only flight."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.runtime.digital_twin_sitl_execution_result import (
    DigitalTwinSITLExecutionResult,
    digital_twin_sitl_execution_result_ref,
)
from src.runtime.digital_twin_sitl_mavlink_upload import (
    DEFAULT_DIGITAL_TWIN_SITL_ENDPOINTS,
    DigitalTwinSITLMissionUploadReceipt,
    digital_twin_sitl_mission_upload_receipt_ref,
)
from src.runtime.flight_readiness_package import (
    FlightReadinessPackage,
    flight_readiness_package_ref,
)


DIGITAL_TWIN_SITL_ARM_TAKEOFF_RECEIPT_SCHEMA_VERSION = (
    "digital_twin_sitl_arm_and_takeoff_observed.v1"
)


class DigitalTwinSITLArmTakeoffError(RuntimeError):
    """Raised when Digital Twin SITL ARM/takeoff observation overclaims facts."""


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


def _coerce_endpoint(endpoint: str) -> None:
    if endpoint not in DEFAULT_DIGITAL_TWIN_SITL_ENDPOINTS:
        raise DigitalTwinSITLArmTakeoffError("target_endpoint_not_allowlisted")
    parsed = urlparse(endpoint)
    if parsed.scheme != "udp" or parsed.hostname != "127.0.0.1":
        raise DigitalTwinSITLArmTakeoffError("target_endpoint_not_loopback_udp")


def _as_float(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)


class DigitalTwinSITLArmTakeoffReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_SITL_ARM_TAKEOFF_RECEIPT_SCHEMA_VERSION] = (
        DIGITAL_TWIN_SITL_ARM_TAKEOFF_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str
    flight_readiness_package_ref: str
    same_run_mission_upload_receipt_ref: str
    same_run_execution_result_ref: str
    target_endpoint: str
    arm_command_attempted: bool
    auto_mission_start_attempted: bool
    arm_observed: bool
    auto_mission_mode_observed: bool
    mission_start_observed: bool
    takeoff_observed: bool
    takeoff_altitude_max_m: float = Field(ge=0)
    home_altitude_m: float = Field(ge=0)
    altitude_rise_m: float = Field(ge=0)
    flight_duration_s: float = Field(ge=0)
    telemetry_samples: tuple[dict[str, Any], ...] = ()
    arm_ack_observed: bool = False
    arm_ack_result: int | None = None
    auto_mission_ack_observed: bool = False
    auto_mission_ack_result: int | None = None
    mission_start_ack_observed: bool = False
    mission_start_ack_result: int | None = None
    operator_approved: bool
    server_opt_in: bool
    observed_facts_only: Literal[True] = True
    simulation_only: Literal[True] = True
    loopback_sitl_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    blocked_reasons: tuple[str, ...] = ()
    observed_at: datetime
    receipt_hash: str
    sha256: str

    @field_validator("blocked_reasons", mode="before")
    @classmethod
    def _coerce_reasons(cls, value: Any) -> tuple[str, ...]:
        return tuple(str(item) for item in (value or ()))

    @field_validator("telemetry_samples", mode="before")
    @classmethod
    def _coerce_samples(cls, value: Any) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in (value or ()))

    @field_validator("observed_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_receipt(self) -> "DigitalTwinSITLArmTakeoffReceipt":
        if not self.flight_readiness_package_ref.startswith("flight_readiness_package:"):
            raise DigitalTwinSITLArmTakeoffError(
                "ARM/takeoff receipt requires Flight Readiness Package ref"
            )
        if not self.same_run_mission_upload_receipt_ref.startswith(
            "digital_twin_sitl_mission_upload_receipt:"
        ):
            raise DigitalTwinSITLArmTakeoffError(
                "ARM/takeoff receipt requires mission upload receipt ref"
            )
        if not self.same_run_execution_result_ref.startswith(
            "digital_twin_sitl_execution_result:"
        ):
            raise DigitalTwinSITLArmTakeoffError(
                "ARM/takeoff receipt requires execution result ref"
            )
        if self.receipt_hash != self.sha256:
            raise DigitalTwinSITLArmTakeoffError("ARM/takeoff receipt hash mismatch")
        observed = (
            self.arm_observed
            and self.auto_mission_mode_observed
            and self.mission_start_observed
            and self.takeoff_observed
        )
        if observed:
            if not self.operator_approved or not self.server_opt_in:
                raise DigitalTwinSITLArmTakeoffError(
                    "observed ARM/takeoff requires approval and opt-in"
                )
            if not self.arm_command_attempted or not self.auto_mission_start_attempted:
                raise DigitalTwinSITLArmTakeoffError(
                    "observed ARM/takeoff requires command attempts"
                )
            if self.altitude_rise_m <= 5.0:
                raise DigitalTwinSITLArmTakeoffError(
                    "observed takeoff requires altitude rise above 5m"
                )
            if self.flight_duration_s <= 10.0:
                raise DigitalTwinSITLArmTakeoffError(
                    "observed takeoff requires flight duration above 10s"
                )
        else:
            if not self.blocked_reasons:
                raise DigitalTwinSITLArmTakeoffError(
                    "non-observed ARM/takeoff receipt requires blocked reasons"
                )
        return self


def digital_twin_sitl_arm_takeoff_receipt_ref(
    receipt: DigitalTwinSITLArmTakeoffReceipt,
) -> str:
    return f"digital_twin_sitl_arm_and_takeoff_observed:{receipt.receipt_id}"


def _same_run_refs_match(
    *,
    package: FlightReadinessPackage,
    execution_result: DigitalTwinSITLExecutionResult,
    mission_upload_receipt: DigitalTwinSITLMissionUploadReceipt,
) -> list[str]:
    blocked: list[str] = []
    if package.execution_result_ref != digital_twin_sitl_execution_result_ref(
        execution_result
    ):
        blocked.append("flight_readiness_package_execution_result_ref_mismatch")
    if package.digital_twin_sitl_mission_upload_receipt_ref != (
        digital_twin_sitl_mission_upload_receipt_ref(mission_upload_receipt)
    ):
        blocked.append("flight_readiness_package_mission_upload_ref_mismatch")
    if execution_result.digital_twin_sitl_mission_upload_receipt_ref != (
        digital_twin_sitl_mission_upload_receipt_ref(mission_upload_receipt)
    ):
        blocked.append("execution_result_mission_upload_ref_mismatch")
    return blocked


def build_digital_twin_sitl_arm_takeoff_receipt(
    *,
    flight_readiness_package: FlightReadinessPackage | Mapping[str, Any],
    mission_upload_receipt: DigitalTwinSITLMissionUploadReceipt | Mapping[str, Any],
    execution_result: DigitalTwinSITLExecutionResult | Mapping[str, Any],
    target_endpoint: str,
    operator_approved: bool = False,
    server_opt_in: bool = False,
    observed: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> DigitalTwinSITLArmTakeoffReceipt:
    package = (
        flight_readiness_package
        if isinstance(flight_readiness_package, FlightReadinessPackage)
        else FlightReadinessPackage.model_validate(flight_readiness_package)
    )
    upload = (
        mission_upload_receipt
        if isinstance(mission_upload_receipt, DigitalTwinSITLMissionUploadReceipt)
        else DigitalTwinSITLMissionUploadReceipt.model_validate(mission_upload_receipt)
    )
    execution = (
        execution_result
        if isinstance(execution_result, DigitalTwinSITLExecutionResult)
        else DigitalTwinSITLExecutionResult.model_validate(execution_result)
    )
    blocked: list[str] = []
    try:
        _coerce_endpoint(target_endpoint)
    except DigitalTwinSITLArmTakeoffError as exc:
        blocked.append(str(exc))
    if operator_approved is not True:
        blocked.append("operator_approval_missing")
    if server_opt_in is not True:
        blocked.append("server_opt_in_missing")
    if package.readiness_status != "ready_for_human_hardware_review":
        blocked.append("flight_readiness_package_not_ready")
    if not upload.mission_ack_observed:
        blocked.append("mission_ack_not_observed")
    if not execution.flight_telemetry_observed:
        blocked.append("execution_telemetry_not_observed")
    blocked.extend(
        _same_run_refs_match(
            package=package,
            execution_result=execution,
            mission_upload_receipt=upload,
        )
    )

    facts = dict(observed or {})
    arm_command_attempted = bool(facts.get("arm_command_attempted")) and not blocked
    auto_mission_start_attempted = (
        bool(facts.get("auto_mission_start_attempted")) and not blocked
    )
    arm_observed = bool(facts.get("arm_observed")) and not blocked
    auto_mission_mode_observed = (
        bool(facts.get("auto_mission_mode_observed")) and not blocked
    )
    takeoff_altitude_max_m = _as_float(facts.get("takeoff_altitude_max_m"))
    home_altitude_m = _as_float(facts.get("home_altitude_m"))
    altitude_rise_m = _as_float(
        facts.get("altitude_rise_m"),
        default=max(0.0, takeoff_altitude_max_m - home_altitude_m)
        if home_altitude_m
        else takeoff_altitude_max_m,
    )
    flight_duration_s = _as_float(facts.get("flight_duration_s"))
    mission_start_observed = bool(facts.get("mission_start_observed")) and not blocked
    takeoff_observed = (
        bool(facts.get("takeoff_observed"))
        and altitude_rise_m > 5.0
        and flight_duration_s > 10.0
        and not blocked
    )
    if not arm_observed and arm_command_attempted:
        blocked.append("arm_not_observed")
    if not auto_mission_mode_observed and auto_mission_start_attempted:
        blocked.append("auto_mission_mode_not_observed")
    if not mission_start_observed and auto_mission_start_attempted:
        blocked.append("mission_start_not_observed")
    if not takeoff_observed and arm_command_attempted:
        blocked.append("takeoff_altitude_not_observed")

    observed_at = _utc(now)
    package_ref = flight_readiness_package_ref(package)
    upload_ref = digital_twin_sitl_mission_upload_receipt_ref(upload)
    execution_ref = digital_twin_sitl_execution_result_ref(execution)
    payload = {
        "schema_version": DIGITAL_TWIN_SITL_ARM_TAKEOFF_RECEIPT_SCHEMA_VERSION,
        "flight_readiness_package_ref": package_ref,
        "same_run_mission_upload_receipt_ref": upload_ref,
        "same_run_execution_result_ref": execution_ref,
        "target_endpoint": target_endpoint,
        "arm_command_attempted": arm_command_attempted,
        "auto_mission_start_attempted": auto_mission_start_attempted,
        "arm_observed": arm_observed,
        "auto_mission_mode_observed": auto_mission_mode_observed,
        "mission_start_observed": mission_start_observed,
        "takeoff_observed": takeoff_observed,
        "takeoff_altitude_max_m": takeoff_altitude_max_m,
        "home_altitude_m": home_altitude_m,
        "altitude_rise_m": altitude_rise_m,
        "flight_duration_s": flight_duration_s,
        "telemetry_samples": tuple(facts.get("telemetry_samples") or ()),
        "arm_ack_observed": bool(facts.get("arm_ack_observed")) and not blocked,
        "arm_ack_result": facts.get("arm_ack_result"),
        "auto_mission_ack_observed": (
            bool(facts.get("auto_mission_ack_observed")) and not blocked
        ),
        "auto_mission_ack_result": facts.get("auto_mission_ack_result"),
        "mission_start_ack_observed": (
            bool(facts.get("mission_start_ack_observed")) and not blocked
        ),
        "mission_start_ack_result": facts.get("mission_start_ack_result"),
        "blocked_reasons": tuple(sorted(set(blocked))),
    }
    digest = _content_hash(payload)
    return DigitalTwinSITLArmTakeoffReceipt(
        receipt_id="digital_twin_sitl_arm_takeoff_" + digest[:12],
        flight_readiness_package_ref=package_ref,
        same_run_mission_upload_receipt_ref=upload_ref,
        same_run_execution_result_ref=execution_ref,
        target_endpoint=target_endpoint,
        arm_command_attempted=arm_command_attempted,
        auto_mission_start_attempted=auto_mission_start_attempted,
        arm_observed=arm_observed,
        auto_mission_mode_observed=auto_mission_mode_observed,
        mission_start_observed=mission_start_observed,
        takeoff_observed=takeoff_observed,
        takeoff_altitude_max_m=takeoff_altitude_max_m,
        home_altitude_m=home_altitude_m,
        altitude_rise_m=altitude_rise_m,
        flight_duration_s=flight_duration_s,
        telemetry_samples=tuple(facts.get("telemetry_samples") or ()),
        arm_ack_observed=bool(facts.get("arm_ack_observed")) and not blocked,
        arm_ack_result=facts.get("arm_ack_result"),
        auto_mission_ack_observed=(
            bool(facts.get("auto_mission_ack_observed")) and not blocked
        ),
        auto_mission_ack_result=facts.get("auto_mission_ack_result"),
        mission_start_ack_observed=(
            bool(facts.get("mission_start_ack_observed")) and not blocked
        ),
        mission_start_ack_result=facts.get("mission_start_ack_result"),
        operator_approved=operator_approved,
        server_opt_in=server_opt_in,
        blocked_reasons=tuple(sorted(set(blocked))),
        observed_at=observed_at,
        receipt_hash=digest,
        sha256=digest,
    )


__all__ = [
    "DIGITAL_TWIN_SITL_ARM_TAKEOFF_RECEIPT_SCHEMA_VERSION",
    "DigitalTwinSITLArmTakeoffError",
    "DigitalTwinSITLArmTakeoffReceipt",
    "build_digital_twin_sitl_arm_takeoff_receipt",
    "digital_twin_sitl_arm_takeoff_receipt_ref",
]
