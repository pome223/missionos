from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from src.runtime.runtime_claim_evidence import (
    RuntimeClaimValidationError,
    normalize_runtime_claims,
)


def _evidence(*, exit_code: int = 0, started_at: datetime | None = None) -> dict:
    start = started_at or datetime.now(timezone.utc) - timedelta(seconds=2)
    stdout = "contract stdout"
    stderr = "contract stderr"
    return {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": "subprocess",
        "invocation_target": "contract-test",
        "invocation_started_at": start.isoformat(),
        "invocation_completed_at": (start + timedelta(seconds=1)).isoformat(),
        "invocation_stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "invocation_stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
        "invocation_stdout_preimage": stdout,
        "invocation_stderr_preimage": stderr,
        "invocation_exit_code": exit_code,
    }


def test_common_runtime_claims_require_invocation_evidence() -> None:
    with pytest.raises(
        RuntimeClaimValidationError,
        match="physical_execution_invoked_in_runtime_requires_runtime_invocation_evidence",
    ):
        normalize_runtime_claims(
            {
                "physical_execution_invoked_in_runtime": True,
                "progress_counted": True,
            }
        )


def test_progress_requires_at_least_one_runtime_claim() -> None:
    with pytest.raises(
        RuntimeClaimValidationError,
        match="artifact_only_runtime_claim_cannot_count_progress",
    ):
        normalize_runtime_claims({"progress_counted": True})


def test_success_claim_rejects_nonzero_runtime_exit() -> None:
    with pytest.raises(
        RuntimeClaimValidationError,
        match="completion_claimed_in_runtime_requires_zero_exit",
    ):
        normalize_runtime_claims(
            {
                "completion_claimed_in_runtime": True,
                "progress_counted": True,
                "runtime_invocation_evidence": _evidence(exit_code=9),
            }
        )


def test_runtime_evidence_rejects_reversed_timestamps() -> None:
    evidence = _evidence()
    evidence["invocation_completed_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat()
    with pytest.raises(
        RuntimeClaimValidationError,
        match="completed_before_started",
    ):
        normalize_runtime_claims(
            {
                "completion_claimed_in_runtime": True,
                "runtime_invocation_evidence": evidence,
            }
        )


def test_runtime_evidence_rejects_unverifiable_or_mismatched_hashes() -> None:
    missing_preimage = _evidence()
    missing_preimage.pop("invocation_stdout_preimage")
    with pytest.raises(
        RuntimeClaimValidationError,
        match="invocation_stdout_sha256_preimage_required",
    ):
        normalize_runtime_claims(
            {"runtime_invocation_evidence": missing_preimage}
        )

    mismatched = _evidence()
    mismatched["invocation_stdout_preimage"] = "tampered stdout"
    with pytest.raises(
        RuntimeClaimValidationError,
        match="invocation_stdout_sha256_mismatch",
    ):
        normalize_runtime_claims({"runtime_invocation_evidence": mismatched})


def test_valid_runtime_completion_can_count_progress() -> None:
    normalized = normalize_runtime_claims(
        {
            "completion_claimed_in_runtime": True,
            "progress_counted": True,
            "runtime_invocation_evidence": _evidence(),
        }
    )

    assert normalized["completion_claimed_in_runtime"] is True
    assert normalized["runtime_invocation_evidence"][
        "invocation_output_hashes_verified"
    ] is True
    assert normalized["runtime_claim_validation"]["runtime_claims"] == [
        "completion_claimed"
    ]
