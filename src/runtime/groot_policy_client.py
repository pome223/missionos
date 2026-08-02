"""Fail-closed client boundary for the observed GR00T N1.5 policy service.

The service returns a model-inferred action proposal. This module creates no
approval, dispatch, execution, progress, safe-stop, or verifier authority.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Protocol

import numpy as np


GROOT_REPOSITORY_REVISION = "4af2b622892f7dcb5aae5a3fb70bcb02dc217b96"
GROOT_MODEL_SNAPSHOT = "869830fc749c35f34771aa5209f923ac57e4564e"
GROOT_REQUEST_SCHEMA = "missionos_groot_n1_5_policy_request.v1"
GROOT_RESPONSE_SCHEMA = "missionos_groot_n1_5_policy_response.v1"
GROOT_PROPOSAL_SCHEMA = "missionos_groot_action_chunk_proposal.v1"
GROOT_SIM_FRESHNESS_POLICY_ID = (
    "missionos.groot.fixed-base-arm-only.sim-freshness"
)
GROOT_SIM_FRESHNESS_POLICY_VERSION = "2026-07-28"
GROOT_SIM_FRESHNESS_POLICY_RATIONALE = (
    "experimental_sim_policy_from_one_observed_round_trip_not_manufacturer_limit"
)

_REQUEST_ARRAYS = {
    "state.left_arm": ((1, 7), np.dtype("float64")),
    "state.left_hand": ((1, 6), np.dtype("float64")),
    "state.right_arm": ((1, 7), np.dtype("float64")),
    "state.right_hand": ((1, 6), np.dtype("float64")),
    "video.ego_view": ((1, 256, 256, 3), np.dtype("uint8")),
}
_INSTRUCTION_KEY = "annotation.human.action.task_description"
_RESPONSE_ARRAYS = {
    "action.left_arm": ((16, 7), np.dtype("float32")),
    "action.left_hand": ((16, 6), np.dtype("float32")),
    "action.right_arm": ((16, 7), np.dtype("float32")),
    "action.right_hand": ((16, 6), np.dtype("float32")),
}
_MAX_RESPONSE_BYTES = 1_000_000


class GrootPolicyBoundaryError(ValueError):
    """The request, response, binding, or transport failed closed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GrootPolicyTransport(Protocol):
    def get_action(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class GrootFreshnessPolicy:
    """Authority artifact for observation age, not a robot physical limit."""

    policy_id: str
    policy_version: str
    maximum_observation_age_seconds: float
    rationale: str
    policy_sha256: str

    def material(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "maximum_observation_age_seconds": (
                self.maximum_observation_age_seconds
            ),
            "rationale": self.rationale,
            "execution_scope": "sim",
            "execution_profile": "fixed_base_arm_only",
        }


def build_groot_sim_freshness_policy() -> GrootFreshnessPolicy:
    base = GrootFreshnessPolicy(
        policy_id=GROOT_SIM_FRESHNESS_POLICY_ID,
        policy_version=GROOT_SIM_FRESHNESS_POLICY_VERSION,
        maximum_observation_age_seconds=3.0,
        rationale=GROOT_SIM_FRESHNESS_POLICY_RATIONALE,
        policy_sha256="",
    )
    digest = hashlib.sha256(
        json.dumps(
            base.material(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return GrootFreshnessPolicy(
        policy_id=base.policy_id,
        policy_version=base.policy_version,
        maximum_observation_age_seconds=(
            base.maximum_observation_age_seconds
        ),
        rationale=base.rationale,
        policy_sha256=digest,
    )


@dataclass(frozen=True)
class GrootPolicyBinding:
    """Source-bound facts required to request one action proposal."""

    instruction_ref: str
    preparation_sha256: str
    observed_at: str
    freshness_deadline: str
    freshness_policy: GrootFreshnessPolicy
    policy_revision: str = GROOT_REPOSITORY_REVISION
    model_snapshot: str = GROOT_MODEL_SNAPSHOT
    request_schema: str = GROOT_REQUEST_SCHEMA
    response_schema: str = GROOT_RESPONSE_SCHEMA


@dataclass(frozen=True)
class GrootActionChunkProposal:
    """A validated model output with no execution authority."""

    schema_version: str
    verification_basis: str
    actions: Mapping[str, np.ndarray]
    instruction_ref: str
    preparation_sha256: str
    policy_revision: str
    model_snapshot: str
    request_sha256: str
    observation_sha256: str
    response_sha256: str
    observed_at: str
    freshness_deadline: str
    response_received_at: str
    approval_created: bool = False
    dispatch_authority_created: bool = False
    dispatch_request_sent: bool = False
    execution_claimed: bool = False
    progress_claimed: bool = False
    safe_stop_claimed: bool = False
    completion_claimed: bool = False
    physical_execution_invoked: bool = False


@dataclass(frozen=True)
class GrootPolicyResponseAssessment:
    """Independent response-schema and temporal-freshness results."""

    response_received: bool
    observation_observed_at: str
    request_started_at: str
    response_received_at: str
    response_schema_valid: bool
    response_schema_reason: str | None
    temporal_freshness_valid: bool
    temporal_freshness_reason: str | None
    request_sha256: str
    observation_sha256: str
    response_sha256: str | None
    proposal: GrootActionChunkProposal | None


def _parse_utc(value: str, *, reason: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise GrootPolicyBoundaryError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GrootPolicyBoundaryError(reason)
    return parsed.astimezone(timezone.utc)


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _validate_binding(
    binding: GrootPolicyBinding,
    *,
    evaluated_at: datetime,
) -> None:
    if not binding.instruction_ref.strip():
        raise GrootPolicyBoundaryError("groot_instruction_ref_missing")
    if not _valid_sha256(binding.preparation_sha256):
        raise GrootPolicyBoundaryError("groot_preparation_digest_invalid")
    if binding.policy_revision != GROOT_REPOSITORY_REVISION:
        raise GrootPolicyBoundaryError("groot_policy_revision_not_supported")
    if binding.model_snapshot != GROOT_MODEL_SNAPSHOT:
        raise GrootPolicyBoundaryError("groot_model_snapshot_not_supported")
    if binding.request_schema != GROOT_REQUEST_SCHEMA:
        raise GrootPolicyBoundaryError("groot_request_schema_not_supported")
    if binding.response_schema != GROOT_RESPONSE_SCHEMA:
        raise GrootPolicyBoundaryError("groot_response_schema_not_supported")
    policy = binding.freshness_policy
    if (
        policy.policy_id != GROOT_SIM_FRESHNESS_POLICY_ID
        or policy.policy_version != GROOT_SIM_FRESHNESS_POLICY_VERSION
        or policy.rationale != GROOT_SIM_FRESHNESS_POLICY_RATIONALE
        or policy.maximum_observation_age_seconds != 3.0
    ):
        raise GrootPolicyBoundaryError("groot_freshness_policy_not_supported")
    expected_policy_sha256 = hashlib.sha256(
        json.dumps(
            policy.material(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if policy.policy_sha256 != expected_policy_sha256:
        raise GrootPolicyBoundaryError("groot_freshness_policy_digest_invalid")

    observed_at = _parse_utc(
        binding.observed_at,
        reason="groot_observed_at_invalid",
    )
    deadline = _parse_utc(
        binding.freshness_deadline,
        reason="groot_freshness_deadline_invalid",
    )
    if (
        not isinstance(evaluated_at, datetime)
        or evaluated_at.tzinfo is None
        or evaluated_at.utcoffset() is None
    ):
        raise GrootPolicyBoundaryError("groot_evaluated_at_invalid")
    evaluated = evaluated_at.astimezone(timezone.utc)
    if deadline <= observed_at:
        raise GrootPolicyBoundaryError("groot_freshness_window_invalid")
    expected_deadline = observed_at + timedelta(
        seconds=policy.maximum_observation_age_seconds
    )
    if deadline != expected_deadline:
        raise GrootPolicyBoundaryError(
            "groot_freshness_deadline_policy_mismatch"
        )
    if observed_at > evaluated:
        raise GrootPolicyBoundaryError("groot_observation_from_future")
    if evaluated > deadline:
        raise GrootPolicyBoundaryError("groot_observation_stale")


def _validate_array(
    value: Any,
    *,
    shape: tuple[int, ...],
    dtype: np.dtype[Any],
    reason_prefix: str,
) -> np.ndarray:
    if not isinstance(value, np.ndarray):
        raise GrootPolicyBoundaryError(f"{reason_prefix}_type_invalid")
    if value.shape != shape:
        raise GrootPolicyBoundaryError(f"{reason_prefix}_shape_invalid")
    if value.dtype != dtype:
        raise GrootPolicyBoundaryError(f"{reason_prefix}_dtype_invalid")
    if np.issubdtype(value.dtype, np.number) and not np.isfinite(value).all():
        raise GrootPolicyBoundaryError(f"{reason_prefix}_non_finite")
    return value


def validate_groot_request(payload: Mapping[str, Any]) -> None:
    """Validate the exact unit-independent observed request contract."""

    expected = {*_REQUEST_ARRAYS, _INSTRUCTION_KEY}
    if set(payload) != expected:
        raise GrootPolicyBoundaryError("groot_request_fields_invalid")
    instruction = payload[_INSTRUCTION_KEY]
    if (
        not isinstance(instruction, list)
        or len(instruction) != 1
        or not isinstance(instruction[0], str)
        or not instruction[0].strip()
    ):
        raise GrootPolicyBoundaryError("groot_instruction_empty_or_invalid")
    for field, (shape, dtype) in _REQUEST_ARRAYS.items():
        _validate_array(
            payload[field],
            shape=shape,
            dtype=dtype,
            reason_prefix=f"groot_request_{field.replace('.', '_')}",
        )


def validate_groot_response(payload: Mapping[str, Any]) -> None:
    """Validate the exact unit-independent observed response contract."""

    if set(payload) != set(_RESPONSE_ARRAYS):
        raise GrootPolicyBoundaryError("groot_response_fields_invalid")
    for field, (shape, dtype) in _RESPONSE_ARRAYS.items():
        _validate_array(
            payload[field],
            shape=shape,
            dtype=dtype,
            reason_prefix=f"groot_response_{field.replace('.', '_')}",
        )


def _array_digest_payload(value: np.ndarray) -> dict[str, Any]:
    return {
        "dtype": str(value.dtype),
        "shape": list(value.shape),
        "sha256": hashlib.sha256(value.tobytes(order="C")).hexdigest(),
    }


def _digest_payload(payload: Mapping[str, Any], *, include_instruction: bool) -> str:
    material: dict[str, Any] = {}
    for key in sorted(payload):
        if key == _INSTRUCTION_KEY:
            if include_instruction:
                material[key] = list(payload[key])
            continue
        material[key] = _array_digest_payload(payload[key])
    encoded = json.dumps(
        material,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _freeze_actions(payload: Mapping[str, Any]) -> Mapping[str, np.ndarray]:
    frozen: dict[str, np.ndarray] = {}
    for field in sorted(_RESPONSE_ARRAYS):
        action = np.array(payload[field], copy=True)
        action.flags.writeable = False
        frozen[field] = action
    return MappingProxyType(frozen)


class GrootZmqPolicyTransport:
    """Compatible ZMQ transport with an actual send/receive deadline."""

    def __init__(
        self,
        *,
        endpoint: str,
        timeout_ms: int,
        api_token: str | None = None,
    ) -> None:
        if not endpoint.startswith("tcp://"):
            raise GrootPolicyBoundaryError("groot_transport_endpoint_invalid")
        if timeout_ms <= 0:
            raise GrootPolicyBoundaryError("groot_transport_timeout_invalid")
        self.endpoint = endpoint
        self.timeout_ms = timeout_ms
        self.api_token = api_token
        self._runtime_invocation_evidence: dict[str, Any] | None = None

    @staticmethod
    def _dependencies() -> tuple[Any, Any]:
        try:
            import msgpack
            import zmq
        except ImportError as exc:
            raise GrootPolicyBoundaryError(
                "groot_transport_dependencies_missing"
            ) from exc
        return msgpack, zmq

    @staticmethod
    def _encode_numpy(value: Any) -> Any:
        if isinstance(value, np.ndarray):
            buffer = io.BytesIO()
            np.save(buffer, value, allow_pickle=False)
            return {"__ndarray_class__": True, "as_npy": buffer.getvalue()}
        raise TypeError(f"unsupported payload type: {type(value).__name__}")

    @staticmethod
    def _decode_numpy(value: Mapping[str, Any]) -> Any:
        if value.get("__ndarray_class__") is True:
            encoded = value.get("as_npy")
            if not isinstance(encoded, bytes):
                raise GrootPolicyBoundaryError(
                    "groot_transport_ndarray_encoding_invalid"
                )
            return np.load(io.BytesIO(encoded), allow_pickle=False)
        return dict(value)

    def get_action(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        msgpack, zmq = self._dependencies()
        started_at = datetime.now(timezone.utc)
        context = zmq.Context()
        socket = context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        socket.setsockopt(zmq.MAXMSGSIZE, _MAX_RESPONSE_BYTES)
        try:
            socket.connect(self.endpoint)
            request: dict[str, Any] = {
                "endpoint": "get_action",
                "data": dict(payload),
            }
            if self.api_token:
                request["api_token"] = self.api_token
            socket.send(msgpack.packb(request, default=self._encode_numpy))
            encoded_response = socket.recv()
            if len(encoded_response) > _MAX_RESPONSE_BYTES:
                raise GrootPolicyBoundaryError(
                    "groot_transport_response_too_large"
                )
            response = msgpack.unpackb(
                encoded_response,
                object_hook=self._decode_numpy,
            )
        except zmq.Again as exc:
            raise GrootPolicyBoundaryError("groot_transport_timeout") from exc
        except GrootPolicyBoundaryError:
            raise
        except Exception as exc:
            raise GrootPolicyBoundaryError("groot_transport_failed") from exc
        finally:
            socket.close()
            context.term()
        if not isinstance(response, Mapping):
            raise GrootPolicyBoundaryError("groot_transport_response_invalid")
        if "error" in response:
            raise GrootPolicyBoundaryError("groot_policy_service_error")
        completed_at = datetime.now(timezone.utc)
        try:
            request_sha256 = _digest_payload(
                payload,
                include_instruction=True,
            )
            response_sha256 = _digest_payload(
                response,
                include_instruction=False,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return response
        stdout_preimage = json.dumps(
            {
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        stderr_preimage = ""
        self._runtime_invocation_evidence = {
            "schema_version": "runtime_invocation_evidence.v1",
            "invocation_kind": "llm_api",
            "invocation_target": f"groot_n1_5:{self.endpoint}",
            "invocation_started_at": started_at.isoformat(),
            "invocation_completed_at": completed_at.isoformat(),
            "invocation_exit_code": 0,
            "invocation_stdout_preimage": stdout_preimage,
            "invocation_stdout_sha256": hashlib.sha256(
                stdout_preimage.encode("utf-8")
            ).hexdigest(),
            "invocation_stderr_preimage": stderr_preimage,
            "invocation_stderr_sha256": hashlib.sha256(
                stderr_preimage.encode("utf-8")
            ).hexdigest(),
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "policy_revision": GROOT_REPOSITORY_REVISION,
            "model_snapshot": GROOT_MODEL_SNAPSHOT,
            "execution_scope": "sim",
        }
        return response

    def collect_runtime_invocation_evidence(
        self,
    ) -> tuple[dict[str, Any], ...]:
        """Return source-bound evidence for the most recent successful call."""

        if self._runtime_invocation_evidence is None:
            return ()
        return (dict(self._runtime_invocation_evidence),)


def request_groot_action_chunk(
    *,
    transport: GrootPolicyTransport,
    payload: Mapping[str, Any],
    binding: GrootPolicyBinding,
    clock: Callable[[], datetime] | None = None,
) -> GrootActionChunkProposal:
    """Return a validated proposal without creating downstream authority."""

    assessment = assess_groot_action_chunk(
        transport=transport,
        payload=payload,
        binding=binding,
        clock=clock,
    )
    if assessment.proposal is not None:
        return assessment.proposal
    raise GrootPolicyBoundaryError(
        assessment.response_schema_reason
        or assessment.temporal_freshness_reason
        or "groot_policy_response_unverified"
    )


def assess_groot_action_chunk(
    *,
    transport: GrootPolicyTransport,
    payload: Mapping[str, Any],
    binding: GrootPolicyBinding,
    clock: Callable[[], datetime] | None = None,
) -> GrootPolicyResponseAssessment:
    """Assess one response without letting schema validity wash freshness."""

    clock = clock or (lambda: datetime.now(timezone.utc))
    requested_at = clock()
    _validate_binding(binding, evaluated_at=requested_at)
    requested_at_text = (
        requested_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    validate_groot_request(payload)
    request_sha256 = _digest_payload(payload, include_instruction=True)
    observation_sha256 = _digest_payload(payload, include_instruction=False)
    response = transport.get_action(payload)
    received_at = clock()
    if received_at < requested_at:
        raise GrootPolicyBoundaryError("groot_clock_regressed")
    received_at_text = (
        received_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    schema_reason: str | None = None
    try:
        validate_groot_response(response)
    except GrootPolicyBoundaryError as exc:
        schema_reason = exc.reason
    freshness_reason: str | None = None
    try:
        _validate_binding(binding, evaluated_at=received_at)
    except GrootPolicyBoundaryError as exc:
        freshness_reason = exc.reason
    response_sha256 = (
        _digest_payload(response, include_instruction=False)
        if schema_reason is None
        else None
    )
    proposal = (
        GrootActionChunkProposal(
            schema_version=GROOT_PROPOSAL_SCHEMA,
            verification_basis="model_inferred",
            actions=_freeze_actions(response),
            instruction_ref=binding.instruction_ref,
            preparation_sha256=binding.preparation_sha256,
            policy_revision=binding.policy_revision,
            model_snapshot=binding.model_snapshot,
            request_sha256=request_sha256,
            observation_sha256=observation_sha256,
            response_sha256=str(response_sha256),
            observed_at=binding.observed_at,
            freshness_deadline=binding.freshness_deadline,
            response_received_at=received_at_text,
        )
        if schema_reason is None and freshness_reason is None
        else None
    )
    return GrootPolicyResponseAssessment(
        response_received=True,
        observation_observed_at=binding.observed_at,
        request_started_at=requested_at_text,
        response_received_at=received_at_text,
        response_schema_valid=schema_reason is None,
        response_schema_reason=schema_reason,
        temporal_freshness_valid=freshness_reason is None,
        temporal_freshness_reason=freshness_reason,
        request_sha256=request_sha256,
        observation_sha256=observation_sha256,
        response_sha256=response_sha256,
        proposal=proposal,
    )


__all__ = [
    "GROOT_MODEL_SNAPSHOT",
    "GROOT_PROPOSAL_SCHEMA",
    "GROOT_REPOSITORY_REVISION",
    "GROOT_REQUEST_SCHEMA",
    "GROOT_RESPONSE_SCHEMA",
    "GROOT_SIM_FRESHNESS_POLICY_ID",
    "GROOT_SIM_FRESHNESS_POLICY_RATIONALE",
    "GROOT_SIM_FRESHNESS_POLICY_VERSION",
    "GrootActionChunkProposal",
    "GrootFreshnessPolicy",
    "GrootPolicyBinding",
    "GrootPolicyBoundaryError",
    "GrootPolicyResponseAssessment",
    "GrootPolicyTransport",
    "GrootZmqPolicyTransport",
    "assess_groot_action_chunk",
    "build_groot_sim_freshness_policy",
    "request_groot_action_chunk",
    "validate_groot_request",
    "validate_groot_response",
]
