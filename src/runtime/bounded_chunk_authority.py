"""Backend-neutral authority for one bounded, closed-loop dispatch.

The core owns only authority and loop semantics. Backends still own model
invocation, action mapping, execution, evidence collection, and verification.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


BOUNDED_CHUNK_AUTHORITY_SCHEMA_VERSION = "missionos_bounded_chunk_authority.v1"
CONTINUE_VERDICT = "continue"


class BoundedChunkExecutionError(RuntimeError):
    """Preserve completed chunk evidence when a later chunk aborts."""

    def __init__(self, *, cause: Exception, partial_result: Mapping[str, Any]) -> None:
        super().__init__("bounded_chunk_execution_failed")
        self.error_type = type(cause).__name__
        self.partial_result = dict(partial_result)


class DispatchLedger(Protocol):
    def claim_dispatch_ref(
        self,
        *,
        dispatch_ref: str,
        request_payload: Mapping[str, Any],
        correlation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def mark_dispatch_send_started(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
    ) -> dict[str, Any]: ...

    def record_dispatch_receipt(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def record_unknown_dispatch_outcome(
        self,
        *,
        dispatch_ref: str,
        claim_id: str,
        error_type: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class BoundedChunkAuthorityPolicy:
    authority_contract_sha256: str
    maximum_chunks: int
    terminal_verdicts: tuple[str, ...]
    verifier_passed_verdicts: tuple[str, ...]
    completion_verdicts: tuple[str, ...]
    budget_exhausted_verdict: str = "budget_exhausted"

    def __post_init__(self) -> None:
        if len(self.authority_contract_sha256) != 64:
            raise ValueError("bounded_chunk_authority_contract_sha256_invalid")
        if isinstance(self.maximum_chunks, bool) or self.maximum_chunks <= 0:
            raise ValueError("bounded_chunk_authority_maximum_chunks_invalid")
        if not str(self.budget_exhausted_verdict or "").strip():
            raise ValueError("bounded_chunk_budget_exhausted_verdict_invalid")
        terminal = _validated_verdicts(self.terminal_verdicts, "terminal")
        verifier_passed = _validated_verdicts(
            self.verifier_passed_verdicts,
            "verifier_passed",
        )
        completion = _validated_verdicts(self.completion_verdicts, "completion")
        if CONTINUE_VERDICT in terminal:
            raise ValueError("bounded_chunk_continue_cannot_be_terminal")
        if not verifier_passed.issubset(terminal):
            raise ValueError("bounded_chunk_verifier_passed_not_terminal")
        if not completion.issubset(verifier_passed):
            raise ValueError("bounded_chunk_completion_not_verifier_passed")


@dataclass(frozen=True)
class BoundedChunkStep:
    observation: Any
    verdict: str
    evidence: Mapping[str, Any]
    effect_observed: bool

    def __post_init__(self) -> None:
        if not str(self.verdict or "").strip():
            raise ValueError("bounded_chunk_verdict_required")
        if not isinstance(self.effect_observed, bool):
            raise ValueError("bounded_chunk_effect_observed_must_be_bool")


@dataclass(frozen=True)
class BoundedChunkAuthoritySession:
    policy: BoundedChunkAuthorityPolicy
    dispatch_ref: str
    claim_id: str
    dispatch_ledger: DispatchLedger

    @property
    def chunk_indices(self) -> range:
        return range(self.policy.maximum_chunks)

    def record_unknown(self, error: Exception) -> None:
        self.dispatch_ledger.record_unknown_dispatch_outcome(
            dispatch_ref=self.dispatch_ref,
            claim_id=self.claim_id,
            error_type=type(error).__name__,
        )

    def complete(
        self,
        *,
        status: str,
        chunks_executed: int,
        effect_observed: bool,
    ) -> dict[str, Any]:
        if isinstance(chunks_executed, bool) or not 0 <= chunks_executed <= (
            self.policy.maximum_chunks
        ):
            raise ValueError("bounded_chunk_chunks_executed_invalid")
        if status != self.policy.budget_exhausted_verdict and status not in set(
            self.policy.terminal_verdicts
        ):
            raise ValueError(f"bounded_chunk_completion_status_not_authorized:{status}")
        receipt_payload = {
            "dispatch_ref": self.dispatch_ref,
            "send_attempted": True,
            "ack_observed": False,
            "effect_observed": effect_observed,
            "verifier_passed": status in set(self.policy.verifier_passed_verdicts),
            "completion_claimed": status in set(self.policy.completion_verdicts),
            "status": status,
            "chunks_executed": chunks_executed,
            "maximum_chunks": self.policy.maximum_chunks,
        }
        receipt = self.dispatch_ledger.record_dispatch_receipt(
            dispatch_ref=self.dispatch_ref,
            claim_id=self.claim_id,
            receipt=receipt_payload,
        )
        return {**receipt_payload, "receipt_present": receipt.get("receipt_present") is True}


def _validated_verdicts(values: Sequence[str], label: str) -> set[str]:
    normalized = {str(value or "").strip() for value in values}
    if not normalized or "" in normalized:
        raise ValueError(f"bounded_chunk_{label}_verdicts_invalid")
    return normalized


def begin_bounded_chunk_authority(
    *,
    policy: BoundedChunkAuthorityPolicy,
    dispatch_ref: str,
    dispatch_request_payload: Mapping[str, Any],
    correlation: Mapping[str, Any] | None,
    dispatch_ledger: DispatchLedger,
    contract_binding_field: str = "authority_contract_sha256",
) -> BoundedChunkAuthoritySession:
    normalized_dispatch_ref = str(dispatch_ref or "").strip()
    if not normalized_dispatch_ref:
        raise ValueError("bounded_chunk_dispatch_ref_required")
    request_payload = dict(dispatch_request_payload)
    binding_field = str(contract_binding_field or "").strip()
    if not binding_field:
        raise ValueError("bounded_chunk_contract_binding_field_required")
    if request_payload.get(binding_field) != policy.authority_contract_sha256:
        raise ValueError("bounded_chunk_dispatch_contract_binding_mismatch")
    claim = dispatch_ledger.claim_dispatch_ref(
        dispatch_ref=normalized_dispatch_ref,
        request_payload=request_payload,
        correlation=dict(correlation or {}),
    )
    if claim.get("send_permitted") is not True:
        raise ValueError(f"bounded_chunk_dispatch_not_permitted:{claim.get('idempotency_status')}")
    claim_id = str(claim["claim_id"])
    send_start = dispatch_ledger.mark_dispatch_send_started(
        dispatch_ref=normalized_dispatch_ref,
        claim_id=claim_id,
    )
    if send_start.get("send_permitted") is not True:
        raise ValueError("bounded_chunk_dispatch_send_not_started")
    return BoundedChunkAuthoritySession(
        policy=policy,
        dispatch_ref=normalized_dispatch_ref,
        claim_id=claim_id,
        dispatch_ledger=dispatch_ledger,
    )


def run_bounded_chunk_authority(
    *,
    policy: BoundedChunkAuthorityPolicy,
    dispatch_ref: str,
    dispatch_request_payload: Mapping[str, Any],
    correlation: Mapping[str, Any] | None,
    dispatch_ledger: DispatchLedger,
    initial_observation: Any,
    execute_and_verify_chunk: Callable[[Any, int], BoundedChunkStep],
    contract_binding_field: str = "authority_contract_sha256",
) -> dict[str, Any]:
    """Run one ledger-claimed dispatch for at most ``maximum_chunks`` chunks."""

    session = begin_bounded_chunk_authority(
        policy=policy,
        dispatch_ref=dispatch_ref,
        dispatch_request_payload=dispatch_request_payload,
        correlation=correlation,
        dispatch_ledger=dispatch_ledger,
        contract_binding_field=contract_binding_field,
    )

    observation = initial_observation
    chunks: list[dict[str, Any]] = []
    status = policy.budget_exhausted_verdict
    terminal_verdicts = set(policy.terminal_verdicts)
    try:
        for chunk_index in session.chunk_indices:
            step = execute_and_verify_chunk(observation, chunk_index)
            if not isinstance(step, BoundedChunkStep):
                raise RuntimeError("bounded_chunk_step_type_invalid")
            if step.verdict != CONTINUE_VERDICT and step.verdict not in terminal_verdicts:
                raise RuntimeError(f"bounded_chunk_verdict_not_authorized:{step.verdict}")
            observation = step.observation
            chunks.append(
                {
                    "chunk_index": chunk_index,
                    "verdict": step.verdict,
                    "effect_observed": step.effect_observed,
                    "evidence": dict(step.evidence),
                }
            )
            if step.verdict != CONTINUE_VERDICT:
                status = step.verdict
                break

        receipt_payload = session.complete(
            status=status,
            chunks_executed=len(chunks),
            effect_observed=any(chunk["effect_observed"] for chunk in chunks),
        )
    except Exception as error:
        session.record_unknown(error)
        partial_result = {
            "schema_version": BOUNDED_CHUNK_AUTHORITY_SCHEMA_VERSION,
            "authority_contract_sha256": policy.authority_contract_sha256,
            "dispatch_ref": session.dispatch_ref,
            "status": "aborted_on_chunk_error",
            "chunks_executed": len(chunks),
            "maximum_chunks": policy.maximum_chunks,
            "chunk_results": chunks,
            "final_observation": observation,
            "effect_observed": any(chunk["effect_observed"] for chunk in chunks),
            "verifier_passed": False,
            "completion_claimed": False,
            "dispatch_receipt_present": False,
            "execution_error_type": type(error).__name__,
        }
        raise BoundedChunkExecutionError(
            cause=error,
            partial_result=partial_result,
        ) from error

    return {
        "schema_version": BOUNDED_CHUNK_AUTHORITY_SCHEMA_VERSION,
        "authority_contract_sha256": policy.authority_contract_sha256,
        "dispatch_ref": session.dispatch_ref,
        "status": status,
        "chunks_executed": len(chunks),
        "maximum_chunks": policy.maximum_chunks,
        "chunk_results": chunks,
        "final_observation": observation,
        "effect_observed": receipt_payload["effect_observed"],
        "verifier_passed": receipt_payload["verifier_passed"],
        "completion_claimed": receipt_payload["completion_claimed"],
        "dispatch_receipt_present": receipt_payload["receipt_present"],
    }


__all__ = [
    "BOUNDED_CHUNK_AUTHORITY_SCHEMA_VERSION",
    "CONTINUE_VERDICT",
    "BoundedChunkAuthorityPolicy",
    "BoundedChunkAuthoritySession",
    "BoundedChunkExecutionError",
    "BoundedChunkStep",
    "DispatchLedger",
    "begin_bounded_chunk_authority",
    "run_bounded_chunk_authority",
]
