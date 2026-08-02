"""Fail-closed GR00T arm-chunk admission and controller evidence bridge.

MissionOS validates one complete model-produced chunk, then hands the chunk to
an external controller in one call.  The controller owns the 20 Hz loop,
handoff-state continuity check, simulator command application, and readback.
This module does not implement low-level control and creates no approval,
completion, delivery, or physical-execution authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import math
import os
import selectors
import shlex
import subprocess
from types import MappingProxyType
from typing import Any, Protocol

import numpy as np
from missionos_core import (
    EvidenceOrigin,
    EvidenceSourceRef,
    ExecutionEnvelopeValidation,
    FeasibilityStatus,
    HardwareExecutionMode,
    VerificationBasis,
    VerificationItem,
    VerificationItemStatus,
    aggregate_verification_items,
    canonical_sha256,
)

from src.runtime.groot_policy_client import GrootActionChunkProposal


GROOT_ARM_CONTROLLER_POLICY_SCHEMA = (
    "missionos_groot_arm_controller_policy.v1"
)
GROOT_ARM_CONTROLLER_REQUEST_SCHEMA = (
    "missionos_groot_arm_controller_request.v1"
)
GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA = (
    "missionos_groot_arm_controller_receipt.v1"
)
GROOT_ARM_CONTROLLER_RESULT_SCHEMA = (
    "missionos_groot_arm_controller_result.v1"
)
GROOT_ARM_CONTROLLER_COMMAND_ENV = "GROOT_ARM_CONTROLLER_COMMAND"
GROOT_ARM_CONTROLLER_SMOKE_ENV = "RUN_MISSIONOS_GROOT_ARM_CONTROLLER_SMOKE"
GROOT_ARM_CONTROLLER_TIMEOUT_ENV = "GROOT_ARM_CONTROLLER_TIMEOUT_S"
GROOT_ROBOCASA_REVISION = "4840e671596f93ca03651524b9f72ffb1aadfeff"
GROOT_ROBOSUITE_REVISION = "75a4c9f4d242c1b7fe7c7fc247b564ec5d8550a2"
GROOT_ROBOCASA_ENVIRONMENT_ID = (
    "robocasa_gr1_arms_only_fourier_hands/"
    "Tabletop_GR1ArmsOnlyFourierHands_Env"
)
GROOT_ROBOT_DESCRIPTION_SHA256 = (
    "c2806641a8e41f10d1daba7b56e2409da37496720deb5a27420b6195bc8fc60d"
)
GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD = 0.25
GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS = 0.05
GROOT_FIXED_BASE_ARM_ONLY_PROFILE = "fixed_base_arm_only"

ARM_FIELDS = ("action.left_arm", "action.right_arm")
HAND_FIELDS = ("action.left_hand", "action.right_hand")
MODEL_ACTION_FIELDS = (
    "action.left_arm",
    "action.left_hand",
    "action.right_arm",
    "action.right_hand",
)
MODEL_ACTION_SHAPES = ((16, 7), (16, 6), (16, 7), (16, 6))
ARM_JOINT_COUNT = 14
CHUNK_STEPS = 16
SAMPLE_PERIOD_SECONDS = 0.05

ARM_STRUCTURE_ITEM = "groot_arm_chunk_structure"
ARM_FRESHNESS_ITEM = "groot_arm_chunk_freshness"
ARM_BINDING_ITEM = "groot_arm_chunk_bindings"
ARM_SCOPE_ITEM = "groot_fixed_base_arm_only_scope"
ARM_RANGE_ITEM = "groot_arm_joint_ranges"
ARM_ENVELOPE_ITEM = "groot_arm_execution_envelope"
ARM_TRANSFORM_ITEM = "groot_arm_transform_authorized"
ARM_CONTROLLER_DYNAMIC_LIMITS_ITEM = (
    "groot_arm_controller_dynamic_limits_enforced"
)
ARM_HANDOFF_ITEM = "groot_arm_handoff_continuity"
ARM_ACK_ITEM = "groot_arm_controller_ack"
ARM_PROGRESS_ITEM = "groot_arm_controller_progress"
ARM_APPLIED_ITEM = "groot_arm_applied_command_identity"
ARM_EFFECT_ITEM = "groot_arm_effect_observed"
ARM_SAFE_STOP_ITEM = "groot_arm_safe_stop_within_remaining_horizon"
HAND_MAPPING_ITEM = "groot_hand_mapping"
HAND_BOUND_ITEM = "groot_hand_policy_bound"
HAND_APPLIED_ITEM = "groot_hand_applied_command"
HAND_EFFECT_ITEM = "groot_hand_effect_observed"

ARM_ADMISSION_ITEMS = (
    ARM_STRUCTURE_ITEM,
    ARM_FRESHNESS_ITEM,
    ARM_BINDING_ITEM,
    ARM_SCOPE_ITEM,
    ARM_RANGE_ITEM,
    ARM_ENVELOPE_ITEM,
    ARM_TRANSFORM_ITEM,
)
ARM_RESULT_ITEMS = (
    ARM_CONTROLLER_DYNAMIC_LIMITS_ITEM,
    ARM_HANDOFF_ITEM,
    ARM_ACK_ITEM,
    ARM_PROGRESS_ITEM,
    ARM_APPLIED_ITEM,
    ARM_EFFECT_ITEM,
)
HAND_REQUIRED_ITEMS = (
    HAND_MAPPING_ITEM,
    HAND_BOUND_ITEM,
    HAND_APPLIED_ITEM,
    HAND_EFFECT_ITEM,
)


def groot_robocasa_controller_configuration_material() -> dict[str, Any]:
    """Return the exact opt-in simulator/controller selection."""

    return {
        "environment_id": GROOT_ROBOCASA_ENVIRONMENT_ID,
        "robocasa_revision": GROOT_ROBOCASA_REVISION,
        "robosuite_revision": GROOT_ROBOSUITE_REVISION,
        "mujoco_version": "3.2.6",
        "numpy_version": "1.26.4",
        "robot": "GR1ArmsOnlyFourierHands",
        "execution_profile": GROOT_FIXED_BASE_ARM_ONLY_PROFILE,
        "base_mobility": "fixed",
        "governed_body_parts": ["left_arm", "right_arm"],
        "balance_coupling_governed": False,
        "whole_body_safety_claimed": False,
        "controller_config": "default_gr1.json",
        "controller_override": {
            "left_gripper_enabled": False,
            "right_gripper_enabled": False,
            "require_gripper_actuator_ctrl_unchanged": True,
        },
        "arm_controller_input_type": "absolute",
        "arm_unit": "rad",
        "controller_dynamic_limits": {
            "ownership": "controller",
            "maximum_handoff_delta_rad": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
            ),
            "maximum_handoff_state_age_seconds": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
            ),
            "source_backed_arm_velocity_limit": None,
            "source_backed_arm_acceleration_limit": None,
            "source_backed_arm_jerk_limit": None,
            "source_status": (
                "not_declared_or_enforced_by_pinned_robot_controller_sources"
            ),
        },
        "sample_rate_hz": 20.0,
        "chunk_steps": 16,
        "hand_actuation_allowed": False,
        "execution_scope": "sim",
    }


def groot_robocasa_controller_configuration_sha256() -> str:
    return canonical_sha256(groot_robocasa_controller_configuration_material())


class GrootArmBridgeError(RuntimeError):
    """A controller transport or response could not satisfy the contract."""


class GrootArmTransformationKind(str, Enum):
    """Closed transformation vocabulary; values have no implied safety."""

    ARM_ONLY_PROJECTION = "arm_only_projection"
    IDENTITY = "identity"
    JOINT_LIMIT_CLAMP = "joint_limit_clamp"
    LINEAR_INTERPOLATION = "linear_interpolation"
    DELTA_RATE_LIMIT = "delta_rate_limit"
    SAMPLE_SELECTION = "sample_selection"


@dataclass(frozen=True)
class GrootArmTransformationStep:
    kind: GrootArmTransformationKind | str
    parameters: Mapping[str, Any]
    input_fields: tuple[str, ...]
    output_fields: tuple[str, ...]
    input_shapes: tuple[tuple[int, int], ...]
    output_shapes: tuple[tuple[int, int], ...]
    unit: str
    input_rate_hz: float
    output_rate_hz: float
    composition_index: int
    implementation_id: str
    implementation_version: str
    implementation_configuration_sha256: str
    policy_source_sha256: str

    def material(self) -> dict[str, Any]:
        return {
            "kind": (
                self.kind.value
                if isinstance(self.kind, GrootArmTransformationKind)
                else str(self.kind)
            ),
            "parameters": dict(self.parameters),
            "input_fields": list(self.input_fields),
            "output_fields": list(self.output_fields),
            "input_shapes": [list(shape) for shape in self.input_shapes],
            "output_shapes": [list(shape) for shape in self.output_shapes],
            "unit": self.unit,
            "input_rate_hz": self.input_rate_hz,
            "output_rate_hz": self.output_rate_hz,
            "composition_index": self.composition_index,
            "implementation_id": self.implementation_id,
            "implementation_version": self.implementation_version,
            "implementation_configuration_sha256": (
                self.implementation_configuration_sha256
            ),
            "policy_source_sha256": self.policy_source_sha256,
        }


@dataclass(frozen=True)
class GrootArmTransformation:
    transformation_id: str
    transformation_version: str
    steps: tuple[GrootArmTransformationStep, ...]

    def material(self) -> dict[str, Any]:
        return {
            "transformation_id": self.transformation_id,
            "transformation_version": self.transformation_version,
            "steps": [step.material() for step in self.steps],
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.material())


@dataclass(frozen=True)
class GrootArmControllerPolicy:
    """Policy-owned source-backed arm semantics and hard bounds."""

    policy_id: str
    policy_version: str
    joint_names: tuple[str, ...]
    lower_position_rad: tuple[float, ...]
    upper_position_rad: tuple[float, ...]
    position_bounds_source_sha256: str
    authorized_transformations: tuple[GrootArmTransformation, ...]
    execution_scope: HardwareExecutionMode = HardwareExecutionMode.SIM
    sample_period_seconds: float = SAMPLE_PERIOD_SECONDS
    chunk_steps: int = CHUNK_STEPS
    hand_actuation_prohibited: bool = True
    execution_envelope_policy_sha256: str = ""
    schema_version: str = GROOT_ARM_CONTROLLER_POLICY_SCHEMA

    def material(self) -> dict[str, Any]:
        material = asdict(self)
        material["execution_scope"] = self.execution_scope.value
        material["authorized_transformations"] = [
            transformation.material()
            for transformation in self.authorized_transformations
        ]
        return material

    @property
    def digest(self) -> str:
        return canonical_sha256(self.material())


@dataclass(frozen=True)
class GrootArmExecutionContext:
    """Authority and preparation bindings that the adapter may not invent."""

    instruction_ref: str
    approval_ref: str
    expected_preparation_sha256: str
    controller_configuration_sha256: str
    safety_configuration_sha256: str
    policy: GrootArmControllerPolicy
    envelope_validation: ExecutionEnvelopeValidation
    execution_profile: str = GROOT_FIXED_BASE_ARM_ONLY_PROFILE
    balance_coupling_governed: bool = False
    whole_body_safety_claimed: bool = False
    instruction_requires_hand_actuation: bool = False
    verifier_assumptions: tuple[str, ...] = (
        "controller_and_readback_share_simulator_process",
        "controller_and_readback_share_host_clock",
    )


@dataclass(frozen=True)
class GrootArmControllerRequest:
    """One chunk handoff; the external controller owns its real-time loop."""

    request_id: str
    instruction_ref: str
    approval_ref: str
    admitted_chunk_sha256: str
    transformed_chunk_sha256: str
    transformation_sha256: str
    transformation_material: Mapping[str, Any]
    controller_policy_sha256: str
    controller_policy_material: Mapping[str, Any]
    controller_configuration_sha256: str
    safety_configuration_sha256: str
    proposal_received_at: str
    handoff_deadline: str
    remaining_valid_horizon_seconds: float
    sample_period_seconds: float
    left_arm_rad: tuple[tuple[float, ...], ...]
    right_arm_rad: tuple[tuple[float, ...], ...]
    hand_actuation_allowed: bool = False
    execution_profile: str = GROOT_FIXED_BASE_ARM_ONLY_PROFILE
    balance_coupling_governed: bool = False
    whole_body_safety_claimed: bool = False
    execution_scope: HardwareExecutionMode = HardwareExecutionMode.SIM
    schema_version: str = GROOT_ARM_CONTROLLER_REQUEST_SCHEMA
    physical_execution_invoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["execution_scope"] = self.execution_scope.value
        return value


@dataclass(frozen=True)
class GrootArmControllerReceipt:
    """Machine-observed controller facts; safe readback is not command identity."""

    request_id: str
    admitted_chunk_sha256: str
    transformed_chunk_sha256: str
    transformation_sha256: str
    controller_policy_sha256: str
    controller_configuration_sha256: str
    proposal_received_at: str
    handoff_deadline: str
    remaining_valid_horizon_seconds_at_handoff: float
    handoff_observed_at: str
    handoff_state_age_seconds: float
    handoff_left_arm_rad: tuple[float, ...]
    handoff_right_arm_rad: tuple[float, ...]
    controller_ack_observed: bool
    progress_samples_observed: int
    progress_samples: tuple[Mapping[str, Any], ...]
    progress_observed_at: str | None
    progress_source_sha256: str | None
    applied_left_arm_rad: tuple[tuple[float, ...], ...] | None
    applied_right_arm_rad: tuple[tuple[float, ...], ...] | None
    applied_command_sha256: str | None
    effect_observed_at: str | None
    effect_left_arm_rad: tuple[float, ...] | None
    effect_right_arm_rad: tuple[float, ...] | None
    effect_source_id: str
    effect_source_sha256: str
    hand_command_applied: bool
    dynamic_limits_configuration_sha256: str | None = None
    dynamic_limits_observation_sha256: str | None = None
    dynamic_limits_evidence_origin: str = EvidenceOrigin.UNVERIFIED.value
    dynamic_limits_enforced: bool = False
    execution_profile: str = GROOT_FIXED_BASE_ARM_ONLY_PROFILE
    balance_coupling_governed: bool = False
    whole_body_safety_claimed: bool = False
    envelope_violation_observed: bool = False
    safe_stop_requested: bool = False
    safe_stop_ack_observed: bool = False
    safe_stop_effect_observed: bool = False
    stop_detection_latency_seconds: float | None = None
    stop_effect_latency_seconds: float | None = None
    remaining_chunk_horizon_seconds: float | None = None
    execution_scope: HardwareExecutionMode = HardwareExecutionMode.SIM
    schema_version: str = GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA
    physical_execution_invoked: bool = False
    task_completion_claimed: bool = False

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> GrootArmControllerReceipt:
        known = {field.name for field in cls.__dataclass_fields__.values()}
        if set(value) - known:
            raise GrootArmBridgeError("groot_controller_receipt_fields_invalid")
        normalized = dict(value)
        try:
            normalized["execution_scope"] = HardwareExecutionMode(
                normalized.get("execution_scope")
            )
        except (TypeError, ValueError) as exc:
            raise GrootArmBridgeError(
                "groot_controller_receipt_execution_scope_invalid"
            ) from exc
        try:
            return cls(**normalized)
        except (TypeError, ValueError) as exc:
            raise GrootArmBridgeError(
                "groot_controller_receipt_invalid"
            ) from exc


@dataclass(frozen=True)
class GrootArmControllerResult:
    status: FeasibilityStatus
    verification_basis: VerificationBasis
    reasons: tuple[str, ...]
    admitted_chunk_sha256: str
    transformed_chunk_sha256: str | None
    applied_command_sha256: str | None
    verification_items: tuple[VerificationItem, ...]
    required_verification_item_ids: tuple[str, ...]
    evidence_sources: Mapping[str, EvidenceSourceRef]
    controller_request_sent: bool
    safe_stop_requested: bool
    safe_stop_ack_observed: bool
    safe_stop_effect_observed: bool
    verifier_assumptions: tuple[str, ...]
    execution_profile: str = GROOT_FIXED_BASE_ARM_ONLY_PROFILE
    balance_coupling_governed: bool = False
    whole_body_safety_claimed: bool = False
    chunk_age_at_handoff_seconds: float | None = None
    remaining_horizon_at_handoff_seconds: float | None = None
    observed_handoff_max_abs_joint_delta_rad: float | None = None
    observed_handoff_state_age_seconds: float | None = None
    handoff_continuity_passed: bool | None = None
    schema_version: str = GROOT_ARM_CONTROLLER_RESULT_SCHEMA
    approval_created: bool = False
    task_completion_claimed: bool = False
    physical_execution_invoked: bool = False


class GrootArmController(Protocol):
    """External simulator controller boundary; called once per whole chunk."""

    def observe_handoff_state(self) -> Mapping[str, Any]: ...

    def apply_arm_chunk(
        self,
        request: GrootArmControllerRequest,
    ) -> GrootArmControllerReceipt | Mapping[str, Any]: ...


class GrootArmControllerCommandClient:
    """Opt-in subprocess boundary for a simulator-owned arm controller."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        raw_command = os.environ.get(GROOT_ARM_CONTROLLER_COMMAND_ENV, "")
        self.command = tuple(command or shlex.split(raw_command))
        raw_timeout = os.environ.get(GROOT_ARM_CONTROLLER_TIMEOUT_ENV, "")
        try:
            self.timeout_seconds = (
                float(raw_timeout)
                if timeout_seconds is None and raw_timeout
                else float(timeout_seconds or 5.0)
            )
        except (TypeError, ValueError) as exc:
            raise GrootArmBridgeError(
                "groot_controller_timeout_invalid"
            ) from exc
        if not self.command:
            raise GrootArmBridgeError("groot_controller_command_missing")
        if self.timeout_seconds <= 0:
            raise GrootArmBridgeError("groot_controller_timeout_invalid")
        self._runtime_invocations: list[dict[str, Any]] = []

    def apply_arm_chunk(
        self,
        request: GrootArmControllerRequest,
    ) -> GrootArmControllerReceipt:
        if (
            os.environ.get(GROOT_ARM_CONTROLLER_SMOKE_ENV, "")
            .strip()
            .lower()
            not in {"1", "true", "yes", "on"}
        ):
            raise GrootArmBridgeError("groot_controller_smoke_not_enabled")
        started_at = datetime.now(timezone.utc)
        try:
            completed = subprocess.run(
                self.command,
                input=json.dumps(
                    request.to_dict(),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GrootArmBridgeError("groot_controller_timeout") from exc
        completed_at = datetime.now(timezone.utc)
        self._runtime_invocations.append(
            {
                "schema_version": "runtime_invocation_evidence.v1",
                "invocation_kind": "subprocess",
                "invocation_target": "groot_arm_controller:apply_chunk",
                "invocation_started_at": started_at.isoformat(),
                "invocation_completed_at": completed_at.isoformat(),
                "invocation_exit_code": completed.returncode,
                "invocation_stdout_sha256": hashlib.sha256(
                    completed.stdout.encode("utf-8")
                ).hexdigest(),
                "invocation_stderr_sha256": hashlib.sha256(
                    completed.stderr.encode("utf-8")
                ).hexdigest(),
                "bridge_command_sha256": canonical_sha256(
                    {"command": list(self.command)}
                ),
                "execution_scope": HardwareExecutionMode.SIM.value,
                "physical_execution_invoked": False,
            }
        )
        if completed.returncode != 0:
            raise GrootArmBridgeError("groot_controller_command_failed")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise GrootArmBridgeError(
                "groot_controller_receipt_not_json"
            ) from exc
        if not isinstance(response, Mapping):
            raise GrootArmBridgeError("groot_controller_receipt_not_object")
        return GrootArmControllerReceipt.from_dict(response)

    def collect_runtime_invocation_evidence(
        self,
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(MappingProxyType(dict(item)) for item in self._runtime_invocations)


class GrootArmControllerProcessClient:
    """Persistent JSON-lines controller process with one preloaded simulator."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        timeout_seconds: float = 10.0,
    ) -> None:
        if not command or timeout_seconds <= 0:
            raise GrootArmBridgeError(
                "groot_controller_process_configuration_invalid"
            )
        self.command = (*command, "--server")
        self.timeout_seconds = timeout_seconds
        self._runtime_invocations: list[dict[str, Any]] = []
        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready = self._read_response()
        if ready.get("status") != "ready":
            self.close()
            raise GrootArmBridgeError("groot_controller_process_not_ready")

    def _read_response(self) -> Mapping[str, Any]:
        if self._process.stdout is None:
            raise GrootArmBridgeError("groot_controller_process_stdout_missing")
        selector = selectors.DefaultSelector()
        selector.register(self._process.stdout, selectors.EVENT_READ)
        try:
            if not selector.select(self.timeout_seconds):
                raise GrootArmBridgeError("groot_controller_process_timeout")
            line = self._process.stdout.readline()
        finally:
            selector.close()
        if not line:
            raise GrootArmBridgeError("groot_controller_process_exited")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GrootArmBridgeError(
                "groot_controller_process_response_not_json"
            ) from exc
        if not isinstance(response, Mapping):
            raise GrootArmBridgeError(
                "groot_controller_process_response_not_object"
            )
        if response.get("error"):
            raise GrootArmBridgeError("groot_controller_process_error")
        return response

    def _request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._process.poll() is not None or self._process.stdin is None:
            raise GrootArmBridgeError("groot_controller_process_not_running")
        started_at = datetime.now(timezone.utc)
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._process.stdin.write(encoded + "\n")
        self._process.stdin.flush()
        response = self._read_response()
        completed_at = datetime.now(timezone.utc)
        self._runtime_invocations.append(
            {
                "schema_version": "runtime_invocation_evidence.v1",
                "invocation_kind": "subprocess",
                "invocation_target": "groot_arm_controller_process",
                "invocation_started_at": started_at.isoformat(),
                "invocation_completed_at": completed_at.isoformat(),
                "request_sha256": hashlib.sha256(
                    encoded.encode("utf-8")
                ).hexdigest(),
                "response_sha256": canonical_sha256(dict(response)),
                "bridge_command_sha256": canonical_sha256(
                    {"command": list(self.command)}
                ),
                "execution_scope": HardwareExecutionMode.SIM.value,
                "physical_execution_invoked": False,
            }
        )
        return response

    def observe_handoff_state(self) -> Mapping[str, Any]:
        return self._request({"action": "observe_handoff_state"})

    def observe_policy_input(self) -> Mapping[str, Any]:
        return self._request({"action": "observe_policy_input"})

    def exercise_safe_stop(self) -> Mapping[str, Any]:
        return self._request({"action": "exercise_safe_stop"})

    def apply_arm_chunk(
        self,
        request: GrootArmControllerRequest,
    ) -> GrootArmControllerReceipt:
        return GrootArmControllerReceipt.from_dict(
            self._request(request.to_dict())
        )

    def collect_runtime_invocation_evidence(
        self,
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            MappingProxyType(dict(item))
            for item in self._runtime_invocations
        )

    def close(self) -> None:
        if self._process.poll() is None:
            try:
                if self._process.stdin is not None:
                    self._process.stdin.write(
                        json.dumps({"action": "shutdown"}) + "\n"
                    )
                    self._process.stdin.flush()
                self._process.wait(timeout=2)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)

    def __enter__(self) -> GrootArmControllerProcessClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _valid_sha256(value: str | None) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _named_arrays_sha256(
    arrays: Sequence[tuple[str, np.ndarray]],
) -> str:
    return canonical_sha256(
        {
            "fields": [
                {
                    "field": field,
                    "dtype": str(value.dtype),
                    "shape": list(value.shape),
                    "content_sha256": hashlib.sha256(
                        np.ascontiguousarray(value).tobytes(order="C")
                    ).hexdigest(),
                }
                for field, value in arrays
            ]
        }
    )


def _array_sha256(left: np.ndarray, right: np.ndarray) -> str:
    return canonical_sha256(
        {
            "dtype": "float64",
            "left_shape": list(left.shape),
            "left_sha256": hashlib.sha256(
                left.astype(np.float64, copy=False).tobytes(order="C")
            ).hexdigest(),
            "right_shape": list(right.shape),
            "right_sha256": hashlib.sha256(
                right.astype(np.float64, copy=False).tobytes(order="C")
            ).hexdigest(),
        }
    )


def _item(
    item_id: str,
    *,
    passed: bool | None,
    evidence_refs: Sequence[str],
    predicate: str,
) -> VerificationItem:
    normalized = None if passed is None else bool(passed)
    return VerificationItem(
        item_id=item_id,
        predicate=predicate,
        status=(
            VerificationItemStatus.PASS
            if normalized is True
            else VerificationItemStatus.FAIL
            if normalized is False
            else VerificationItemStatus.PENDING
        ),
        verification_basis=(
            VerificationBasis.DETERMINISTIC
            if normalized is not None
            else VerificationBasis.UNVERIFIED
        ),
        evidence_refs=tuple(evidence_refs),
    )


def _policy_reasons(policy: GrootArmControllerPolicy) -> list[str]:
    reasons: list[str] = []
    if policy.schema_version != GROOT_ARM_CONTROLLER_POLICY_SCHEMA:
        reasons.append("groot_arm_policy_schema_not_supported")
    if policy.execution_scope is not HardwareExecutionMode.SIM:
        reasons.append("groot_arm_policy_scope_not_sim")
    if len(policy.joint_names) != ARM_JOINT_COUNT:
        reasons.append("groot_arm_policy_joint_mapping_invalid")
    if len(set(policy.joint_names)) != len(policy.joint_names):
        reasons.append("groot_arm_policy_joint_mapping_duplicate")
    if (
        len(policy.lower_position_rad) != ARM_JOINT_COUNT
        or len(policy.upper_position_rad) != ARM_JOINT_COUNT
    ):
        reasons.append("groot_arm_policy_position_bounds_invalid")
    elif any(
        not math.isfinite(lower)
        or not math.isfinite(upper)
        or lower >= upper
        for lower, upper in zip(
            policy.lower_position_rad,
            policy.upper_position_rad,
            strict=True,
        )
    ):
        reasons.append("groot_arm_policy_position_bounds_invalid")
    if (
        not _valid_sha256(policy.position_bounds_source_sha256)
        or policy.position_bounds_source_sha256
        != GROOT_ROBOT_DESCRIPTION_SHA256
    ):
        reasons.append("groot_arm_policy_position_bounds_source_invalid")
    if (
        policy.chunk_steps != CHUNK_STEPS
        or policy.sample_period_seconds != SAMPLE_PERIOD_SECONDS
    ):
        reasons.append("groot_arm_policy_observed_timing_mismatch")
    if not policy.hand_actuation_prohibited:
        reasons.append("groot_arm_policy_hand_actuation_not_prohibited")
    if not _valid_sha256(policy.execution_envelope_policy_sha256):
        reasons.append("groot_arm_execution_envelope_policy_digest_invalid")
    ids = [
        transformation.transformation_id
        for transformation in policy.authorized_transformations
    ]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        reasons.append("groot_arm_policy_transformations_invalid")
    return reasons


def _validate_transform_step(
    step: GrootArmTransformationStep,
    *,
    policy: GrootArmControllerPolicy,
    expected_index: int,
    expected_input_shapes: tuple[tuple[int, int], ...],
    expected_input_rate_hz: float,
) -> str | None:
    try:
        kind = GrootArmTransformationKind(step.kind)
    except ValueError:
        return "groot_arm_transformation_kind_unknown"
    if expected_index == 0:
        projection_valid = (
            kind is GrootArmTransformationKind.ARM_ONLY_PROJECTION
            and step.input_fields == MODEL_ACTION_FIELDS
            and step.output_fields == ARM_FIELDS
            and step.input_shapes == MODEL_ACTION_SHAPES
            and step.output_shapes
            == ((policy.chunk_steps, 7), (policy.chunk_steps, 7))
            and step.unit == "arm_rad_hand_unverified"
            and step.input_rate_hz == expected_input_rate_hz
            and step.output_rate_hz == expected_input_rate_hz
            and step.composition_index == 0
            and bool(step.implementation_id)
            and bool(step.implementation_version)
            and _valid_sha256(step.implementation_configuration_sha256)
            and step.policy_source_sha256
            == policy.execution_envelope_policy_sha256
            and dict(step.parameters)
            == {
                "retained_fields": list(ARM_FIELDS),
                "dropped_fields": list(HAND_FIELDS),
                "drop_authority": "active_policy",
                "drop_reason": "arm_only_controller_has_no_hand_actuators",
                "dropped_values_semantically_verified": False,
                "hand_actuation_prohibited": True,
                "input_dtype": "float32",
                "output_dtype": "float64",
                "dtype_conversion": "exact_value_preserving_widening",
            }
        )
        return (
            None
            if projection_valid
            else "groot_arm_only_projection_contract_invalid"
        )
    common_valid = (
        step.input_fields == ARM_FIELDS
        and step.output_fields == ARM_FIELDS
        and step.input_shapes == expected_input_shapes
        and len(step.output_shapes) == 2
        and all(shape[1] == 7 and shape[0] >= 1 for shape in step.output_shapes)
        and step.unit == "rad"
        and step.input_rate_hz == expected_input_rate_hz
        and math.isfinite(step.output_rate_hz)
        and step.output_rate_hz > 0
        and step.composition_index == expected_index
        and bool(step.implementation_id)
        and bool(step.implementation_version)
        and _valid_sha256(step.implementation_configuration_sha256)
        and step.policy_source_sha256
        == policy.execution_envelope_policy_sha256
    )
    if not common_valid:
        return "groot_arm_transformation_contract_incomplete"
    parameters = dict(step.parameters)
    if kind is GrootArmTransformationKind.IDENTITY:
        mapping = {
            field: list(range(7))
            for field in ARM_FIELDS
        }
        return (
            None
            if parameters == {"dimension_mapping": mapping}
            and step.output_shapes == step.input_shapes
            and step.output_rate_hz == step.input_rate_hz
            else "groot_arm_identity_parameters_invalid"
        )
    if kind is GrootArmTransformationKind.JOINT_LIMIT_CLAMP:
        return "groot_arm_clamp_transformation_controller_owned"
    if kind is GrootArmTransformationKind.LINEAR_INTERPOLATION:
        output_steps = parameters.get("output_steps")
        target_rate = parameters.get("target_rate_hz")
        return (
            None
            if set(parameters)
            == {
                "source_rate_hz",
                "target_rate_hz",
                "interpolation_domain",
                "endpoint_rule",
                "output_steps",
            }
            and isinstance(output_steps, int)
            and not isinstance(output_steps, bool)
            and output_steps >= 2
            and parameters["source_rate_hz"] == step.input_rate_hz
            and target_rate == step.output_rate_hz
            and parameters["interpolation_domain"] == "sample_time"
            and parameters["endpoint_rule"] == "preserve_first_and_last"
            and step.output_shapes == ((output_steps, 7), (output_steps, 7))
            else "groot_arm_linear_interpolation_parameters_invalid"
        )
    if kind is GrootArmTransformationKind.DELTA_RATE_LIMIT:
        return "groot_arm_dynamic_limit_transformation_controller_owned"
    indices = parameters.get("source_indices")
    return (
        None
        if set(parameters)
        == {"source_indices", "output_order", "dropped_index_policy"}
        and isinstance(indices, list)
        and indices
        and all(
            isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < step.input_shapes[0][0]
            for index in indices
        )
        and parameters["output_order"] == "listed"
        and parameters["dropped_index_policy"] == "discard_no_replay"
        and step.output_shapes == ((len(indices), 7), (len(indices), 7))
        and step.output_rate_hz == step.input_rate_hz
        else "groot_arm_sample_selection_parameters_invalid"
    )


def _apply_transformation(
    actions: Mapping[str, np.ndarray],
    *,
    transformation: GrootArmTransformation,
    policy: GrootArmControllerPolicy,
) -> tuple[np.ndarray | None, tuple[str, ...]]:
    output: np.ndarray | None = None
    reasons: list[str] = []
    expected_shapes: tuple[tuple[int, int], ...] = (
        *MODEL_ACTION_SHAPES,
    )
    expected_rate = 1.0 / policy.sample_period_seconds
    for index, step in enumerate(transformation.steps):
        reason = _validate_transform_step(
            step,
            policy=policy,
            expected_index=index,
            expected_input_shapes=expected_shapes,
            expected_input_rate_hz=expected_rate,
        )
        if reason:
            reasons.append(reason)
            continue
        kind = GrootArmTransformationKind(step.kind)
        parameters = dict(step.parameters)
        if kind is GrootArmTransformationKind.ARM_ONLY_PROJECTION:
            output = np.concatenate(
                (
                    np.asarray(actions[ARM_FIELDS[0]], dtype=np.float64),
                    np.asarray(actions[ARM_FIELDS[1]], dtype=np.float64),
                ),
                axis=1,
            )
        elif output is None:
            reasons.append("groot_arm_only_projection_missing")
            continue
        elif kind is GrootArmTransformationKind.IDENTITY:
            continue
        if kind is GrootArmTransformationKind.JOINT_LIMIT_CLAMP:
            reasons.append("groot_arm_clamp_transformation_controller_owned")
        elif kind is GrootArmTransformationKind.LINEAR_INTERPOLATION:
            output_steps = int(parameters["output_steps"])
            old_axis = np.linspace(0.0, 1.0, output.shape[0])
            new_axis = np.linspace(0.0, 1.0, output_steps)
            output = np.stack(
                [
                    np.interp(new_axis, old_axis, output[:, joint])
                    for joint in range(output.shape[1])
                ],
                axis=1,
            )
        elif kind is GrootArmTransformationKind.DELTA_RATE_LIMIT:
            reasons.append(
                "groot_arm_dynamic_limit_transformation_controller_owned"
            )
        elif kind is GrootArmTransformationKind.SAMPLE_SELECTION:
            output = output[
                np.asarray(parameters["source_indices"], dtype=int)
            ]
        expected_shapes = step.output_shapes
        expected_rate = step.output_rate_hz
    if reasons:
        return None, tuple(dict.fromkeys(reasons))
    if (
        output is None
        or output.shape[0] < 1
        or output.shape[1] != ARM_JOINT_COUNT
    ):
        return None, ("groot_arm_transformation_output_shape_invalid",)
    return output, ()


def _position_range_check(
    chunk: np.ndarray,
    *,
    policy: GrootArmControllerPolicy,
) -> bool:
    lower = np.asarray(policy.lower_position_rad)
    upper = np.asarray(policy.upper_position_rad)
    return bool(np.all((chunk >= lower) & (chunk <= upper)))


def _evidence(
    source_id: str,
    *,
    kind: str,
    observed_at: str,
    content_sha256: str,
) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=source_id,
        evidence_kind=kind,
        observed_at=observed_at,
        content_sha256=content_sha256,
        execution_scope=HardwareExecutionMode.SIM,
        origin=EvidenceOrigin.MACHINE_OBSERVED,
    )


def _result(
    *,
    items: Sequence[VerificationItem],
    required: Sequence[str],
    evidence_sources: Mapping[str, EvidenceSourceRef],
    admitted_digest: str,
    transformed_digest: str | None,
    applied_digest: str | None,
    controller_request_sent: bool,
    reasons: Sequence[str],
    context: GrootArmExecutionContext,
    receipt: GrootArmControllerReceipt | None = None,
    observed_handoff_max_abs_joint_delta_rad: float | None = None,
    handoff_continuity_passed: bool | None = None,
) -> GrootArmControllerResult:
    aggregate = aggregate_verification_items(
        items=items,
        required_item_ids=required,
        evidence_sources=evidence_sources,
        expected_execution_scope=HardwareExecutionMode.SIM,
    )
    all_reasons = tuple(
        dict.fromkeys(
            (
                *reasons,
                *aggregate.blocked_reasons,
                *aggregate.unverified_reasons,
            )
        )
    )
    blocked = bool(aggregate.blocked_reasons) or any(
        item.status in {
            VerificationItemStatus.FAIL,
            VerificationItemStatus.BLOCKED,
        }
        for item in items
        if item.item_id in required
    )
    status = (
        FeasibilityStatus.BLOCKED
        if blocked
        else FeasibilityStatus.VERIFIED_FEASIBLE
        if aggregate.positive and not all_reasons
        else FeasibilityStatus.UNVERIFIED
    )
    return GrootArmControllerResult(
        status=status,
        verification_basis=aggregate.verification_basis,
        reasons=all_reasons,
        admitted_chunk_sha256=admitted_digest,
        transformed_chunk_sha256=transformed_digest,
        applied_command_sha256=applied_digest,
        verification_items=tuple(items),
        required_verification_item_ids=tuple(required),
        evidence_sources=MappingProxyType(dict(evidence_sources)),
        controller_request_sent=controller_request_sent,
        safe_stop_requested=bool(receipt and receipt.safe_stop_requested),
        safe_stop_ack_observed=bool(
            receipt and receipt.safe_stop_ack_observed
        ),
        safe_stop_effect_observed=bool(
            receipt and receipt.safe_stop_effect_observed
        ),
        verifier_assumptions=context.verifier_assumptions,
        execution_profile=context.execution_profile,
        balance_coupling_governed=context.balance_coupling_governed,
        whole_body_safety_claimed=context.whole_body_safety_claimed,
        chunk_age_at_handoff_seconds=(
            (
                _parse_utc(receipt.handoff_observed_at)
                - _parse_utc(receipt.proposal_received_at)
            ).total_seconds()
            if receipt is not None
            and _parse_utc(receipt.handoff_observed_at) is not None
            and _parse_utc(receipt.proposal_received_at) is not None
            else None
        ),
        remaining_horizon_at_handoff_seconds=(
            receipt.remaining_valid_horizon_seconds_at_handoff
            if receipt is not None
            else None
        ),
        observed_handoff_max_abs_joint_delta_rad=(
            observed_handoff_max_abs_joint_delta_rad
        ),
        observed_handoff_state_age_seconds=(
            receipt.handoff_state_age_seconds
            if receipt is not None
            else None
        ),
        handoff_continuity_passed=handoff_continuity_passed,
    )


def execute_groot_arm_chunk(
    *,
    proposal: GrootActionChunkProposal,
    context: GrootArmExecutionContext,
    transformation_id: str,
    controller: GrootArmController,
    evaluated_at: datetime | None = None,
) -> GrootArmControllerResult:
    """Admit one arm chunk and verify controller application/readback evidence."""

    evaluated_at = evaluated_at or datetime.now(timezone.utc)
    actions = proposal.actions
    left = np.asarray(actions.get(ARM_FIELDS[0]))
    right = np.asarray(actions.get(ARM_FIELDS[1]))
    left_hand = np.asarray(actions.get(HAND_FIELDS[0]))
    right_hand = np.asarray(actions.get(HAND_FIELDS[1]))
    exact_fields = set(actions) == set(MODEL_ACTION_FIELDS)
    arrays_are_ndarrays = exact_fields and all(
        isinstance(actions[field], np.ndarray)
        for field in MODEL_ACTION_FIELDS
    )
    action_arrays = (
        (MODEL_ACTION_FIELDS[0], left),
        (MODEL_ACTION_FIELDS[1], left_hand),
        (MODEL_ACTION_FIELDS[2], right),
        (MODEL_ACTION_FIELDS[3], right_hand),
    )
    admitted_digest = (
        _named_arrays_sha256(action_arrays)
        if arrays_are_ndarrays
        else canonical_sha256({"invalid_proposal": proposal.request_sha256})
    )
    proposal_ref = f"groot-proposal:{admitted_digest}"
    policy_ref = f"groot-arm-policy:{context.policy.digest}"
    envelope_ref = (
        f"execution-envelope:{context.envelope_validation.policy_binding.policy_sha256}"
    )
    sources: dict[str, EvidenceSourceRef] = {
        proposal_ref: _evidence(
            proposal_ref,
            kind="groot_action_chunk_proposal",
            observed_at=proposal.response_received_at,
            content_sha256=admitted_digest,
        ),
        policy_ref: EvidenceSourceRef(
            source_id=policy_ref,
            evidence_kind="groot_arm_controller_policy",
            observed_at=proposal.response_received_at,
            content_sha256=context.policy.digest,
            execution_scope=HardwareExecutionMode.SIM,
            origin=EvidenceOrigin.AUTHORITY_ARTIFACT,
        ),
        envelope_ref: EvidenceSourceRef(
            source_id=envelope_ref,
            evidence_kind="execution_envelope_validation",
            observed_at=proposal.response_received_at,
            content_sha256=context.envelope_validation.policy_binding.policy_sha256,
            execution_scope=HardwareExecutionMode.SIM,
            origin=EvidenceOrigin.STORED_ARTIFACT,
        ),
    }
    items: list[VerificationItem] = []
    required = list(ARM_ADMISSION_ITEMS)
    policy_reasons = _policy_reasons(context.policy)
    shape_ok = (
        exact_fields
        and arrays_are_ndarrays
        and left.shape == (CHUNK_STEPS, 7)
        and right.shape == (CHUNK_STEPS, 7)
        and left_hand.shape == (CHUNK_STEPS, 6)
        and right_hand.shape == (CHUNK_STEPS, 6)
        and left.dtype == np.dtype("float32")
        and right.dtype == np.dtype("float32")
        and left_hand.dtype == np.dtype("float32")
        and right_hand.dtype == np.dtype("float32")
        and np.isfinite(left).all()
        and np.isfinite(right).all()
        and np.isfinite(left_hand).all()
        and np.isfinite(right_hand).all()
    )
    items.append(
        _item(
            ARM_STRUCTURE_ITEM,
            passed=shape_ok,
            evidence_refs=(proposal_ref,),
            predicate=(
                "exact four-field model chunk is finite float32 with "
                "observed arm [16,7] and hand [16,6] shapes"
            ),
        )
    )
    received = _parse_utc(proposal.response_received_at)
    deadline = _parse_utc(proposal.freshness_deadline)
    chunk_horizon_deadline = (
        received + timedelta(
            seconds=CHUNK_STEPS * SAMPLE_PERIOD_SECONDS
        )
        if received is not None
        else None
    )
    freshness_ok = (
        received is not None
        and deadline is not None
        and chunk_horizon_deadline is not None
        and received <= evaluated_at.astimezone(timezone.utc) <= deadline
        and evaluated_at.astimezone(timezone.utc) <= chunk_horizon_deadline
    )
    items.append(
        _item(
            ARM_FRESHNESS_ITEM,
            passed=freshness_ok,
            evidence_refs=(proposal_ref,),
            predicate="chunk remains inside its source-bound freshness window",
        )
    )
    binding_ok = (
        proposal.instruction_ref == context.instruction_ref
        and bool(context.approval_ref)
        and proposal.preparation_sha256
        == context.expected_preparation_sha256
        and _valid_sha256(context.controller_configuration_sha256)
        and _valid_sha256(context.safety_configuration_sha256)
        and not policy_reasons
    )
    items.append(
        _item(
            ARM_BINDING_ITEM,
            passed=binding_ok,
            evidence_refs=(proposal_ref, policy_ref),
            predicate="instruction, preparation, policy, and controller bindings match",
        )
    )
    scope_ok = bool(
        context.execution_profile == GROOT_FIXED_BASE_ARM_ONLY_PROFILE
        and context.balance_coupling_governed is False
        and context.whole_body_safety_claimed is False
        and context.controller_configuration_sha256
        == groot_robocasa_controller_configuration_sha256()
    )
    items.append(
        _item(
            ARM_SCOPE_ITEM,
            passed=scope_ok,
            evidence_refs=(policy_ref,),
            predicate=(
                "execution is explicitly fixed-base arm-only and makes no "
                "balance or whole-body safety claim"
            ),
        )
    )
    combined = (
        np.concatenate((left, right), axis=1)
        if shape_ok
        else np.empty((0, ARM_JOINT_COUNT), dtype=np.float64)
    )
    range_ok = bool(
        shape_ok
        and not policy_reasons
        and _position_range_check(combined, policy=context.policy)
    )
    items.append(
        _item(
            ARM_RANGE_ITEM,
            passed=range_ok,
            evidence_refs=(proposal_ref, policy_ref),
            predicate=(
                "all arm samples stay within source-bound robot-description "
                "joint ranges"
            ),
        )
    )
    envelope_ok = (
        context.envelope_validation.status
        is FeasibilityStatus.VERIFIED_FEASIBLE
        and context.envelope_validation.policy_binding.policy_sha256
        == context.policy.execution_envelope_policy_sha256
    )
    items.append(
        _item(
            ARM_ENVELOPE_ITEM,
            passed=envelope_ok,
            evidence_refs=(envelope_ref, policy_ref),
            predicate="active execution envelope and safe-stop receipt are verified",
        )
    )
    transformations = {
        item.transformation_id: item
        for item in context.policy.authorized_transformations
    }
    transformation = transformations.get(transformation_id)
    transformed: np.ndarray | None = None
    transform_reasons: tuple[str, ...] = ()
    if transformation is None:
        transform_reasons = ("groot_arm_transformation_not_authorized",)
    elif shape_ok:
        transformed, transform_reasons = _apply_transformation(
            actions,
            transformation=transformation,
            policy=context.policy,
        )
    items.append(
        _item(
            ARM_TRANSFORM_ITEM,
            passed=transformation is not None and not transform_reasons,
            evidence_refs=(proposal_ref, policy_ref),
            predicate="ordered transformation is closed, exact, and policy-authorized",
        )
    )

    hand_required = context.instruction_requires_hand_actuation
    if hand_required:
        required.extend(HAND_REQUIRED_ITEMS)
        for item_id in HAND_REQUIRED_ITEMS:
            items.append(
                _item(
                    item_id,
                    passed=None,
                    evidence_refs=(proposal_ref, policy_ref),
                    predicate="required hand semantics are unresolved",
                )
            )

    preflight = aggregate_verification_items(
        items=items,
        required_item_ids=required,
        evidence_sources=sources,
        expected_execution_scope=HardwareExecutionMode.SIM,
    )
    if not preflight.positive or transformed is None or hand_required:
        return _result(
            items=items,
            required=required,
            evidence_sources=sources,
            admitted_digest=admitted_digest,
            transformed_digest=None,
            applied_digest=None,
            controller_request_sent=False,
            reasons=(*policy_reasons, *transform_reasons),
            context=context,
        )

    if not _position_range_check(transformed, policy=context.policy):
        items[-1] = _item(
            ARM_TRANSFORM_ITEM,
            passed=False,
            evidence_refs=(policy_ref,),
            predicate=(
                "transformed output independently stays inside source-bound "
                "joint ranges"
            ),
        )
        return _result(
            items=items,
            required=required,
            evidence_sources=sources,
            admitted_digest=admitted_digest,
            transformed_digest=None,
            applied_digest=None,
            controller_request_sent=False,
            reasons=("groot_arm_transformed_chunk_outside_envelope",),
            context=context,
        )

    transformed_left = transformed[:, :7]
    transformed_right = transformed[:, 7:]
    transformed_digest = _array_sha256(
        transformed_left,
        transformed_right,
    )
    request = GrootArmControllerRequest(
        request_id=canonical_sha256(
            {
                "admitted_chunk_sha256": admitted_digest,
                "transformed_chunk_sha256": transformed_digest,
                "approval_ref": context.approval_ref,
            }
        ),
        instruction_ref=context.instruction_ref,
        approval_ref=context.approval_ref,
        admitted_chunk_sha256=admitted_digest,
        transformed_chunk_sha256=transformed_digest,
        transformation_sha256=transformation.digest,
        transformation_material=transformation.material(),
        controller_policy_sha256=context.policy.digest,
        controller_policy_material=context.policy.material(),
        controller_configuration_sha256=context.controller_configuration_sha256,
        safety_configuration_sha256=context.safety_configuration_sha256,
        proposal_received_at=proposal.response_received_at,
        handoff_deadline=chunk_horizon_deadline.isoformat().replace(
            "+00:00",
            "Z",
        ),
        remaining_valid_horizon_seconds=max(
            (
                chunk_horizon_deadline
                - evaluated_at.astimezone(timezone.utc)
            ).total_seconds(),
            0.0,
        ),
        sample_period_seconds=context.policy.sample_period_seconds,
        left_arm_rad=tuple(
            tuple(float(value) for value in row)
            for row in transformed_left
        ),
        right_arm_rad=tuple(
            tuple(float(value) for value in row)
            for row in transformed_right
        ),
        execution_profile=context.execution_profile,
        balance_coupling_governed=context.balance_coupling_governed,
        whole_body_safety_claimed=context.whole_body_safety_claimed,
    )
    try:
        raw_receipt = controller.apply_arm_chunk(request)
        receipt = (
            raw_receipt
            if isinstance(raw_receipt, GrootArmControllerReceipt)
            else GrootArmControllerReceipt.from_dict(raw_receipt)
        )
    except Exception:
        items.extend(
            (
                _item(
                    ARM_CONTROLLER_DYNAMIC_LIMITS_ITEM,
                    passed=None,
                    evidence_refs=(proposal_ref,),
                    predicate=(
                        "controller-owned dynamic limits are enforced and "
                        "bound to the controller configuration"
                    ),
                ),
                _item(
                    ARM_HANDOFF_ITEM,
                    passed=None,
                    evidence_refs=(proposal_ref,),
                    predicate="fresh controller handoff state satisfies continuity",
                ),
                _item(
                    ARM_ACK_ITEM,
                    passed=None,
                    evidence_refs=(proposal_ref,),
                    predicate="controller ACK binds the exact request",
                ),
                _item(
                    ARM_PROGRESS_ITEM,
                    passed=None,
                    evidence_refs=(proposal_ref,),
                    predicate="controller progress is separately observed",
                ),
                _item(
                    ARM_APPLIED_ITEM,
                    passed=None,
                    evidence_refs=(proposal_ref,),
                    predicate="controller exposes source-bound applied command",
                ),
                _item(
                    ARM_EFFECT_ITEM,
                    passed=None,
                    evidence_refs=(proposal_ref,),
                    predicate="fresh simulator state observes chunk effect",
                ),
            )
        )
        required.extend(ARM_RESULT_ITEMS)
        return _result(
            items=items,
            required=required,
            evidence_sources=sources,
            admitted_digest=admitted_digest,
            transformed_digest=transformed_digest,
            applied_digest=None,
            controller_request_sent=True,
            reasons=("groot_arm_controller_receipt_unavailable",),
            context=context,
        )

    required.extend(ARM_RESULT_ITEMS)
    receipt_ref = f"groot-controller-receipt:{request.request_id}"
    receipt_material = asdict(receipt)
    receipt_material["execution_scope"] = (
        receipt.execution_scope.value
        if isinstance(receipt.execution_scope, HardwareExecutionMode)
        else str(receipt.execution_scope)
    )
    receipt_digest = canonical_sha256(receipt_material)
    sources[receipt_ref] = _evidence(
        receipt_ref,
        kind="groot_arm_controller_receipt",
        observed_at=receipt.handoff_observed_at,
        content_sha256=receipt_digest,
    )
    handoff = np.asarray(
        (*receipt.handoff_left_arm_rad, *receipt.handoff_right_arm_rad),
        dtype=np.float64,
    )
    dynamic_limits_ref = (
        f"groot-controller-dynamic-limits:{request.request_id}"
    )
    try:
        dynamic_limits_origin = EvidenceOrigin(
            receipt.dynamic_limits_evidence_origin
        )
    except ValueError:
        dynamic_limits_origin = EvidenceOrigin.UNVERIFIED
    if _valid_sha256(receipt.dynamic_limits_observation_sha256):
        sources[dynamic_limits_ref] = EvidenceSourceRef(
            source_id=dynamic_limits_ref,
            evidence_kind="groot_controller_dynamic_limits_observation",
            observed_at=receipt.handoff_observed_at,
            content_sha256=receipt.dynamic_limits_observation_sha256,
            execution_scope=HardwareExecutionMode.SIM,
            origin=dynamic_limits_origin,
        )
    dynamic_limits_binding_matches = (
        receipt.dynamic_limits_configuration_sha256
        == context.controller_configuration_sha256
    )
    observed_handoff_delta = (
        float(np.max(np.abs(transformed[0] - handoff)))
        if handoff.shape == (ARM_JOINT_COUNT,)
        and np.isfinite(handoff).all()
        else None
    )
    dynamic_limits_passed = bool(
        observed_handoff_delta is not None
        and observed_handoff_delta
        <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
        and receipt.handoff_state_age_seconds >= 0
        and receipt.handoff_state_age_seconds
        <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
    )
    expected_dynamic_limits_observation_sha256 = canonical_sha256(
        {
            "controller_configuration_sha256": (
                context.controller_configuration_sha256
            ),
            "maximum_handoff_delta_rad": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
            ),
            "maximum_handoff_state_age_seconds": (
                GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
            ),
            "observed_handoff_delta_rad": observed_handoff_delta,
            "observed_handoff_state_age_seconds": (
                receipt.handoff_state_age_seconds
            ),
            "dynamic_limits_passed": dynamic_limits_passed,
        }
    )
    dynamic_limits_observation_matches = (
        receipt.dynamic_limits_observation_sha256
        == expected_dynamic_limits_observation_sha256
    )
    dynamic_limits_machine_observed = (
        dynamic_limits_origin is EvidenceOrigin.MACHINE_OBSERVED
        and dynamic_limits_ref in sources
    )
    items.append(
        _item(
            ARM_CONTROLLER_DYNAMIC_LIMITS_ITEM,
            passed=(
                bool(
                    receipt.dynamic_limits_enforced
                    and dynamic_limits_binding_matches
                    and dynamic_limits_observation_matches
                )
                if dynamic_limits_machine_observed
                else None
            ),
            evidence_refs=(
                (receipt_ref, dynamic_limits_ref)
                if dynamic_limits_ref in sources
                else (receipt_ref,)
            ),
            predicate=(
                "controller-owned dynamic limits are machine-observed as "
                "enforced and bound to the exact controller configuration"
            ),
        )
    )
    effect_ref = f"groot-effect:{receipt.effect_source_id}"
    if receipt.effect_source_id and _valid_sha256(receipt.effect_source_sha256):
        sources[effect_ref] = _evidence(
            effect_ref,
            kind="simulator_joint_state_readback",
            observed_at=receipt.effect_observed_at or "",
            content_sha256=receipt.effect_source_sha256,
        )
    handoff_at = _parse_utc(receipt.handoff_observed_at)
    receipt_received_at = _parse_utc(receipt.proposal_received_at)
    receipt_deadline = _parse_utc(receipt.handoff_deadline)
    expected_remaining = (
        (chunk_horizon_deadline - handoff_at).total_seconds()
        if handoff_at is not None
        else None
    )
    handoff_ok = (
        receipt.execution_scope is HardwareExecutionMode.SIM
        and receipt.execution_profile == GROOT_FIXED_BASE_ARM_ONLY_PROFILE
        and receipt.balance_coupling_governed is False
        and receipt.whole_body_safety_claimed is False
        and receipt.schema_version == GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA
        and receipt.request_id == request.request_id
        and receipt_received_at == received
        and receipt_deadline == chunk_horizon_deadline
        and handoff_at is not None
        and handoff_at <= chunk_horizon_deadline
        and expected_remaining is not None
        and expected_remaining >= 0
        and math.isclose(
            receipt.remaining_valid_horizon_seconds_at_handoff,
            expected_remaining,
            rel_tol=0.0,
            abs_tol=0.005,
        )
        and receipt.handoff_state_age_seconds >= 0
        and receipt.handoff_state_age_seconds
        <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS
        and handoff.shape == (ARM_JOINT_COUNT,)
        and np.isfinite(handoff).all()
        and np.max(np.abs(transformed[0] - handoff))
        <= GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD
    )
    items.append(
        _item(
            ARM_HANDOFF_ITEM,
            passed=handoff_ok,
            evidence_refs=(receipt_ref, policy_ref),
            predicate="fresh controller handoff state satisfies continuity",
        )
    )
    ack_ok = bool(
        receipt.controller_ack_observed
        and receipt.request_id == request.request_id
    )
    items.append(
        _item(
            ARM_ACK_ITEM,
            passed=ack_ok,
            evidence_refs=(receipt_ref,),
            predicate="controller ACK binds the exact request",
        )
    )
    progress_at = _parse_utc(receipt.progress_observed_at or "")
    progress_digest = canonical_sha256(
        {"samples": [dict(sample) for sample in receipt.progress_samples]}
    )
    progress_ok = bool(
        progress_at is not None
        and receipt.progress_samples_observed == transformed.shape[0]
        and len(receipt.progress_samples) == transformed.shape[0]
        and _valid_sha256(receipt.progress_source_sha256)
        and receipt.progress_source_sha256 == progress_digest
    )
    items.append(
        _item(
            ARM_PROGRESS_ITEM,
            passed=progress_ok,
            evidence_refs=(receipt_ref,),
            predicate="controller progress is separately observed for every sample",
        )
    )
    applied_digest: str | None = None
    applied_ok: bool | None
    if (
        receipt.applied_left_arm_rad is None
        or receipt.applied_right_arm_rad is None
        or receipt.applied_command_sha256 is None
    ):
        applied_ok = None
    else:
        applied_left = np.asarray(receipt.applied_left_arm_rad, dtype=np.float64)
        applied_right = np.asarray(
            receipt.applied_right_arm_rad,
            dtype=np.float64,
        )
        applied_digest = (
            _array_sha256(applied_left, applied_right)
            if applied_left.ndim == 2 and applied_right.ndim == 2
            else None
        )
        applied_ok = bool(
            applied_digest is not None
            and applied_left.shape == transformed_left.shape
            and applied_right.shape == transformed_right.shape
            and np.array_equal(applied_left, transformed_left)
            and np.array_equal(applied_right, transformed_right)
            and applied_digest == transformed_digest
            and receipt.applied_command_sha256 == applied_digest
            and receipt.admitted_chunk_sha256 == admitted_digest
            and receipt.transformed_chunk_sha256 == transformed_digest
            and receipt.transformation_sha256 == transformation.digest
            and receipt.controller_policy_sha256 == context.policy.digest
            and receipt.controller_configuration_sha256
            == context.controller_configuration_sha256
            and not receipt.hand_command_applied
            and not receipt.physical_execution_invoked
        )
    items.append(
        _item(
            ARM_APPLIED_ITEM,
            passed=applied_ok,
            evidence_refs=(receipt_ref, policy_ref),
            predicate="applied command exactly matches approved transformation",
        )
    )
    effect_at = _parse_utc(receipt.effect_observed_at or "")
    effect_left = np.asarray(receipt.effect_left_arm_rad, dtype=np.float64)
    effect_right = np.asarray(receipt.effect_right_arm_rad, dtype=np.float64)
    effect_digest = (
        _array_sha256(
            effect_left.reshape(1, 7),
            effect_right.reshape(1, 7),
        )
        if effect_left.shape == (7,) and effect_right.shape == (7,)
        else None
    )
    effect_ok = bool(
        effect_at is not None
        and effect_left.shape == (7,)
        and effect_right.shape == (7,)
        and np.isfinite(effect_left).all()
        and np.isfinite(effect_right).all()
        and handoff_at is not None
        and effect_at >= handoff_at
        and bool(receipt.effect_source_id)
        and _valid_sha256(receipt.effect_source_sha256)
        and receipt.effect_source_sha256 == effect_digest
        and not receipt.task_completion_claimed
        and not receipt.physical_execution_invoked
    )
    items.append(
        _item(
            ARM_EFFECT_ITEM,
            passed=effect_ok,
            evidence_refs=(effect_ref,),
            predicate="fresh simulator qpos readback observes an effect without semantic completion",
        )
    )
    receipt_reasons: list[str] = []
    if receipt.hand_command_applied:
        receipt_reasons.append("groot_arm_controller_applied_hand_command")
    if receipt.envelope_violation_observed:
        stop_values = (
            receipt.stop_detection_latency_seconds,
            receipt.stop_effect_latency_seconds,
            receipt.remaining_chunk_horizon_seconds,
        )
        stop_timing_ok = (
            all(value is not None and value >= 0 for value in stop_values)
            and receipt.stop_detection_latency_seconds
            + receipt.stop_effect_latency_seconds
            < receipt.remaining_chunk_horizon_seconds
            <= CHUNK_STEPS * SAMPLE_PERIOD_SECONDS
        )
        if not (
            receipt.safe_stop_requested
            and receipt.safe_stop_ack_observed
            and receipt.safe_stop_effect_observed
            and stop_timing_ok
        ):
            receipt_reasons.append(
                "groot_arm_safe_stop_not_observed_within_remaining_horizon"
            )
        required.append(ARM_SAFE_STOP_ITEM)
        items.append(
            _item(
                ARM_SAFE_STOP_ITEM,
                passed=(
                    receipt.safe_stop_requested
                    and receipt.safe_stop_ack_observed
                    and receipt.safe_stop_effect_observed
                    and stop_timing_ok
                ),
                evidence_refs=(receipt_ref, effect_ref),
                predicate="stop request, ACK, and effect fit remaining chunk horizon",
            )
        )
    if receipt.task_completion_claimed:
        receipt_reasons.append("groot_arm_controller_task_completion_claim")
    if receipt.physical_execution_invoked:
        receipt_reasons.append("groot_arm_physical_execution_claim")
    return _result(
        items=items,
        required=required,
        evidence_sources=sources,
        admitted_digest=admitted_digest,
        transformed_digest=transformed_digest,
        applied_digest=applied_digest,
        controller_request_sent=True,
        reasons=receipt_reasons,
        context=context,
        receipt=receipt,
        observed_handoff_max_abs_joint_delta_rad=observed_handoff_delta,
        handoff_continuity_passed=bool(handoff_ok),
    )


__all__ = [
    "ARM_ACK_ITEM",
    "ARM_APPLIED_ITEM",
    "ARM_CONTROLLER_DYNAMIC_LIMITS_ITEM",
    "ARM_EFFECT_ITEM",
    "ARM_HANDOFF_ITEM",
    "ARM_PROGRESS_ITEM",
    "ARM_RANGE_ITEM",
    "ARM_SAFE_STOP_ITEM",
    "ARM_SCOPE_ITEM",
    "GROOT_ARM_CONTROLLER_POLICY_SCHEMA",
    "GROOT_ARM_CONTROLLER_RECEIPT_SCHEMA",
    "GROOT_ARM_CONTROLLER_REQUEST_SCHEMA",
    "GROOT_ARM_CONTROLLER_COMMAND_ENV",
    "GROOT_ARM_CONTROLLER_SMOKE_ENV",
    "GROOT_ARM_CONTROLLER_TIMEOUT_ENV",
    "GROOT_CONTROLLER_MAXIMUM_HANDOFF_DELTA_RAD",
    "GROOT_CONTROLLER_MAXIMUM_HANDOFF_STATE_AGE_SECONDS",
    "GROOT_FIXED_BASE_ARM_ONLY_PROFILE",
    "GROOT_ROBOCASA_ENVIRONMENT_ID",
    "GROOT_ROBOCASA_REVISION",
    "GROOT_ROBOT_DESCRIPTION_SHA256",
    "GROOT_ROBOSUITE_REVISION",
    "GrootArmBridgeError",
    "GrootArmController",
    "GrootArmControllerCommandClient",
    "GrootArmControllerProcessClient",
    "GrootArmControllerPolicy",
    "GrootArmControllerReceipt",
    "GrootArmControllerRequest",
    "GrootArmControllerResult",
    "GrootArmExecutionContext",
    "GrootArmTransformation",
    "GrootArmTransformationKind",
    "GrootArmTransformationStep",
    "execute_groot_arm_chunk",
    "groot_robocasa_controller_configuration_material",
    "groot_robocasa_controller_configuration_sha256",
]
