from __future__ import annotations

import copy

import pytest

from src.runtime.px4_gazebo_route.replay_bundle import (
    build_anonymized_recovery_replay_bundle,
    verify_anonymized_recovery_replay_bundle,
)
from tests.fixtures.px4_recovery_replay import build_fixture_task


pytestmark = pytest.mark.contract


def test_bundle_preserves_two_recovery_cycles_without_private_source_data() -> None:
    bundle = build_anonymized_recovery_replay_bundle(
        build_fixture_task(),
        public_run_ref="fixture-two-recovery-cycles",
    )
    serialized = str(bundle).lower()

    assert bundle["mission"]["recovery_epoch_count"] == 2
    assert bundle["telemetry"]["sample_count"] == 4
    assert bundle["telemetry"]["frame"] == "local_ned"
    assert "task_private_fixture_source" not in serialized
    assert "private-session-not-for-publication" not in serialized
    assert "/users/private" not in serialized
    assert "sk-never-publish" not in serialized
    assert "latitude_deg" not in serialized
    assert "longitude_deg" not in serialized
    assert bundle["terminal_observations"]["delivery_completion_claimed"] is False
    assert bundle["terminal_observations"]["physical_execution_invoked"] is False

    verdict = verify_anonymized_recovery_replay_bundle(bundle)

    assert verdict["verification_status"] == "verified"
    assert verdict["closed_loop_cycle_count"] == 2
    assert verdict["causal_form"] == "Form 3"
    assert verdict["blocking_reasons"] == []
    assert verdict["delivery_completion_claimed"] is False
    assert verdict["physical_execution_invoked"] is False


def test_bundle_verifier_rejects_tampered_outcome() -> None:
    bundle = build_anonymized_recovery_replay_bundle(
        build_fixture_task(),
        public_run_ref="fixture-tampered-recovery",
    )
    tampered = copy.deepcopy(bundle)
    tampered["recovery_epochs"][0]["observation"]["outcome_verification"][
        "target_reached"
    ] = False

    verdict = verify_anonymized_recovery_replay_bundle(tampered)

    assert verdict["verification_status"] == "failed"
    assert "replay_bundle_hash_mismatch" in verdict["blocking_reasons"]
    assert any(
        reason.endswith("recovery_outcome_hash_mismatch")
        for reason in verdict["blocking_reasons"]
    )


def test_bundle_verifier_rejects_task_id_and_local_path() -> None:
    bundle = build_anonymized_recovery_replay_bundle(
        build_fixture_task(),
        public_run_ref="fixture-publication-boundary",
    )
    bundle["private"] = {
        "task_id": "task_should_not_be_public",
        "artifact_path": "/Users/private/evidence.json",
    }

    verdict = verify_anonymized_recovery_replay_bundle(bundle)

    assert verdict["verification_status"] == "failed"
    assert "replay_bundle_publication_boundary_violated" in verdict[
        "blocking_reasons"
    ]


def test_bundle_requires_non_task_public_reference() -> None:
    with pytest.raises(ValueError, match="must not expose a task id"):
        build_anonymized_recovery_replay_bundle(
            build_fixture_task(),
            public_run_ref="task_deadbeef",
        )


def test_legacy_source_marks_missing_dispatch_history_as_limitation() -> None:
    task = build_fixture_task()
    artifacts = task["artifacts"]
    receipts = artifacts.pop("missionos_runtime_recovery_dispatch_receipts")
    artifacts["missionos_runtime_recovery_dispatch_receipt"] = next(
        reversed(receipts.values())
    )

    bundle = build_anonymized_recovery_replay_bundle(
        task,
        public_run_ref="fixture-legacy-source",
    )
    verdict = verify_anonymized_recovery_replay_bundle(bundle)

    assert verdict["verification_status"] == "verified_with_limitations"
    assert verdict["closed_loop_cycle_count"] == 2
    assert "historical_dispatch_receipt_not_preserved_in_source_task" in verdict[
        "limitations"
    ]
