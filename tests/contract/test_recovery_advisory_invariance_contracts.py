"""Recovery advisory invariance and epic-exit contracts from legacy smokes."""

from collections.abc import Mapping

import pytest

from src.runtime.recovery_advisory_context import (
    RecoveryAdvisoryContextError,
    build_recovery_advisory_context,
    build_recovery_advisory_proposal,
    validate_recovery_advisory_refs,
)
from src.runtime.recovery_advisory_epic_exit import (
    RECOVERY_ADVISORY_EPIC_EXIT_SCHEMA_VERSION,
    build_recovery_advisory_epic_exit_result,
)
from src.runtime.recovery_advisory_outcome_invariance import (
    RecoveryAdvisoryOutcomeInvarianceError,
    assert_recovery_outcome_ignores_advisory_context,
    canonical_recovery_outcome_digest,
    current_recovery_advisory_context,
)
from tests.fixtures.delivery_shared_observation import (
    NOW,
    build_shared_observation_bundle,
)
from tests.fixtures.recovery_outcome_cases import (
    load_recovery_outcome_corpus,
    run_logic_only_recovery_outcome_case,
)


FULL_ADVISORY_CONTEXT = (
    {
        "schema_version": "recovery_advisory_context.v1",
        "context_id": "advisory-invariance-fixture",
        "advisory_context_only": True,
    },
)


def test_recovery_outcomes_ignore_advisory_context_and_fail_on_dependency() -> None:
    corpus = load_recovery_outcome_corpus()
    evidence = assert_recovery_outcome_ignores_advisory_context(
        corpus=corpus,
        outcome_runner=run_logic_only_recovery_outcome_case,
        full_advisory_context=FULL_ADVISORY_CONTEXT,
    )

    def advisory_dependent_runner(_case, _advisory_context):
        return {
            "outcome_category": "recovered",
            "advisory_context_count": len(current_recovery_advisory_context()),
        }

    with pytest.raises(
        RecoveryAdvisoryOutcomeInvarianceError,
        match="recovery_outcome_diverged_with_advisory_context",
    ):
        assert_recovery_outcome_ignores_advisory_context(
            corpus=[{"id": "negative-advisory-dependent"}],
            outcome_runner=advisory_dependent_runner,
            full_advisory_context=FULL_ADVISORY_CONTEXT,
        )

    assert len(evidence) == len(corpus) == 8
    assert {item["case_kind"] for item in evidence} == {
        "battery_low_recovery",
        "payload_retry_recovery",
        "telemetry_stale_hold",
        "operator_escalation",
        "blocked_failed_path",
        "observed_facts_incomplete",
    }


def _validate(
    *,
    context,
    bundle,
    recovery_artifacts: Mapping,
) -> None:
    validate_recovery_advisory_refs(
        recovery_advisory_context=context,
        shared_observation_mission_session=bundle.mission,
        shared_observation_vehicle_sessions=bundle.vehicle_sessions,
        shared_observations=[bundle.shared],
        shared_observation_decision_at=NOW,
        max_observation_age_seconds=5.0,
        recovery_artifacts=recovery_artifacts,
    )


def test_recovery_advisory_epic_exit_proves_context_is_never_authority() -> None:
    bundle = build_shared_observation_bundle(received_delay_seconds=0)
    context = build_recovery_advisory_context(
        mission_ref="task:recovery-advisory-epic-exit-contract",
        mission_session_ref=bundle.mission_ref,
        recovery_request_ref="delivery_recovery_request:advisory-epic-exit",
        used_shared_observation_refs=[bundle.shared_ref],
        created_at=NOW,
    )
    proposal = build_recovery_advisory_proposal(
        recovery_request_ref=context.recovery_request_ref,
        recovery_advisory_context=context,
        suppressed_recovery_candidates=[
            {
                "candidate_kind": "retry_without_obstruction_review",
                "suppressing_advisory_ref": bundle.shared_ref,
                "suppression_rationale": "Keep the observed obstruction visible.",
            }
        ],
        created_at=NOW,
    )
    _validate(
        context=context,
        bundle=bundle,
        recovery_artifacts={
            "recovery_advisory_proposal": proposal.model_dump(mode="json")
        },
    )

    corpus = load_recovery_outcome_corpus()
    evidence = assert_recovery_outcome_ignores_advisory_context(
        corpus=corpus,
        outcome_runner=run_logic_only_recovery_outcome_case,
        full_advisory_context=(context.model_dump(mode="json"),),
    )
    case = corpus[0]
    with_advisory = run_logic_only_recovery_outcome_case(case, (context,))
    without_advisory = run_logic_only_recovery_outcome_case(case, ())
    digest_with = canonical_recovery_outcome_digest(with_advisory)
    digest_without = canonical_recovery_outcome_digest(without_advisory)

    context_ref = f"recovery_advisory_context:{context.recovery_advisory_context_id}"
    negative_artifacts = (
        {"delivery_recovery_outcome": {"observed_fact_refs": [context_ref]}},
        {"delivery_scorecard": {"evidence_refs": [context_ref]}},
        {"recovery_success_proof": {"proof_refs": [context_ref]}},
        {"delivery_recovery_outcome": {"outcome_input_refs": [context_ref]}},
        {
            "delivery_recovery_outcome": {
                "recovery_outcome_predicate_overrides": {"safe_landing": True}
            }
        },
        {"bad": {"mavlink_command": "not allowed"}},
    )
    for artifacts in negative_artifacts:
        with pytest.raises(RecoveryAdvisoryContextError):
            _validate(
                context=context,
                bundle=bundle,
                recovery_artifacts=artifacts,
            )

    result = build_recovery_advisory_epic_exit_result(
        recovery_advisory_context=context,
        recovery_advisory_proposal=proposal,
        recovery_outcome_hash_with_advisory=digest_with,
        recovery_outcome_hash_without_advisory=digest_without,
        verifier_invariance_evidence=evidence,
        negative_observed_fact_failed_closed=True,
        negative_scorecard_evidence_failed_closed=True,
        negative_success_proof_failed_closed=True,
        negative_outcome_input_failed_closed=True,
        negative_predicate_change_failed_closed=True,
        negative_command_authority_failed_closed=True,
        created_at=NOW,
    )

    assert result.schema_version == RECOVERY_ADVISORY_EPIC_EXIT_SCHEMA_VERSION
    assert result.recovery_outcome_byte_equal_with_and_without_advisory is True
    assert result.epic_invariant_advisory_context_never_outcome_authority is True
    assert result.dispatch_authority_granted is False
    assert result.external_dispatch_performed is False
    assert result.raw_mavlink_command_allowed is False
    assert result.raw_ros_action_allowed is False
    assert result.gazebo_entity_mutation_allowed is False
    assert result.setpoint_stream_allowed is False
    assert result.actuator_command_allowed is False
    assert result.hardware_target_allowed is False
    assert result.physical_execution_invoked is False
