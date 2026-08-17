from __future__ import annotations

import pytest

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.runtime.bounded_chunk_authority import (
    BoundedChunkExecutionError,
    BoundedChunkAuthorityPolicy,
    BoundedChunkStep,
    run_bounded_chunk_authority,
)


CONTRACT_SHA256 = "a" * 64


def _policy(*, maximum_chunks: int = 3) -> BoundedChunkAuthorityPolicy:
    return BoundedChunkAuthorityPolicy(
        authority_contract_sha256=CONTRACT_SHA256,
        maximum_chunks=maximum_chunks,
        terminal_verdicts=(
            "satisfied",
            "predicate_improved",
            "stopped_on_preservation_invariant",
            "stopped_on_preservation_violation",
        ),
        verifier_passed_verdicts=("satisfied", "predicate_improved"),
        completion_verdicts=("satisfied",),
    )


def _run(tmp_path, verdicts, *, maximum_chunks=3):
    calls = []

    def execute(observation, chunk_index):
        calls.append((observation, chunk_index))
        verdict = verdicts[chunk_index]
        return BoundedChunkStep(
            observation={"version": chunk_index + 1},
            verdict=verdict,
            evidence={"backend": "fixture", "chunk_index": chunk_index},
            effect_observed=True,
        )

    ledger = DispatchAuthorityTable(tmp_path / "dispatch.json")
    result = run_bounded_chunk_authority(
        policy=_policy(maximum_chunks=maximum_chunks),
        dispatch_ref="bounded-chunk:test",
        dispatch_request_payload={
            "authority_contract_sha256": CONTRACT_SHA256,
            "approval_sha256": "b" * 64,
        },
        correlation={"backend": "fixture"},
        dispatch_ledger=ledger,
        initial_observation={"version": 0},
        execute_and_verify_chunk=execute,
    )
    return result, calls, ledger


def test_one_dispatch_authorizes_multiple_chunks_until_verifier_satisfies(tmp_path):
    result, calls, ledger = _run(tmp_path, ["continue", "continue", "satisfied"])

    assert [index for _, index in calls] == [0, 1, 2]
    assert result["status"] == "satisfied"
    assert result["chunks_executed"] == 3
    assert result["verifier_passed"] is True
    assert result["completion_claimed"] is True
    assert result["dispatch_receipt_present"] is True

    replay = ledger.claim_dispatch_ref(
        dispatch_ref="bounded-chunk:test",
        request_payload={
            "authority_contract_sha256": CONTRACT_SHA256,
            "approval_sha256": "b" * 64,
        },
    )
    assert replay["send_permitted"] is False
    assert replay["idempotency_status"] == "existing_receipt"


def test_budget_exhaustion_never_claims_completion(tmp_path):
    result, calls, _ = _run(
        tmp_path,
        ["continue", "continue"],
        maximum_chunks=2,
    )

    assert len(calls) == 2
    assert result["status"] == "budget_exhausted"
    assert result["verifier_passed"] is False
    assert result["completion_claimed"] is False


@pytest.mark.parametrize(
    "verdict",
    ["stopped_on_preservation_invariant", "stopped_on_preservation_violation"],
)
def test_stop_only_verdict_cannot_create_success(tmp_path, verdict):
    result, calls, _ = _run(tmp_path, [verdict])

    assert len(calls) == 1
    assert result["status"] == verdict
    assert result["verifier_passed"] is False
    assert result["completion_claimed"] is False


def test_unknown_verdict_fails_closed_and_blocks_replay(tmp_path):
    ledger = DispatchAuthorityTable(tmp_path / "dispatch.json")
    request = {
        "authority_contract_sha256": CONTRACT_SHA256,
        "approval_sha256": "b" * 64,
    }

    with pytest.raises(BoundedChunkExecutionError) as caught:
        run_bounded_chunk_authority(
            policy=_policy(),
            dispatch_ref="bounded-chunk:test",
            dispatch_request_payload=request,
            correlation=None,
            dispatch_ledger=ledger,
            initial_observation={},
            execute_and_verify_chunk=lambda observation, chunk_index: BoundedChunkStep(
                observation=observation,
                verdict="model_says_success",
                evidence={},
                effect_observed=False,
            ),
        )

    assert caught.value.error_type == "RuntimeError"

    replay = ledger.claim_dispatch_ref(
        dispatch_ref="bounded-chunk:test",
        request_payload=request,
    )
    assert replay["send_permitted"] is False


def test_late_chunk_error_preserves_completed_chunk_evidence(tmp_path):
    def execute(observation, chunk_index):
        if chunk_index == 2:
            raise RuntimeError("local path must not escape: /Users/example/private")
        return BoundedChunkStep(
            observation={"version": chunk_index + 1},
            verdict="continue",
            evidence={"chunk_index": chunk_index},
            effect_observed=True,
        )

    with pytest.raises(BoundedChunkExecutionError) as caught:
        run_bounded_chunk_authority(
            policy=_policy(),
            dispatch_ref="bounded-chunk:partial",
            dispatch_request_payload={
                "authority_contract_sha256": CONTRACT_SHA256,
                "approval_sha256": "b" * 64,
            },
            correlation=None,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / "partial.json"),
            initial_observation={"version": 0},
            execute_and_verify_chunk=execute,
        )

    partial = caught.value.partial_result
    assert str(caught.value) == "bounded_chunk_execution_failed"
    assert partial["status"] == "aborted_on_chunk_error"
    assert partial["chunks_executed"] == 2
    assert [chunk["chunk_index"] for chunk in partial["chunk_results"]] == [0, 1]
    assert partial["execution_error_type"] == "RuntimeError"
    assert "/Users/" not in str(partial)


def test_contract_binding_mismatch_fails_before_ledger_claim(tmp_path):
    ledger = DispatchAuthorityTable(tmp_path / "dispatch.json")

    with pytest.raises(ValueError, match="contract_binding_mismatch"):
        run_bounded_chunk_authority(
            policy=_policy(),
            dispatch_ref="bounded-chunk:test",
            dispatch_request_payload={"authority_contract_sha256": "c" * 64},
            correlation=None,
            dispatch_ledger=ledger,
            initial_observation={},
            execute_and_verify_chunk=lambda observation, chunk_index: pytest.fail(),
        )
