from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from missionos_core import (
    EvidenceOrigin,
    HardwareExecutionMode,
    MissionEvidenceReadiness,
    MissionRuntimeEvidence,
    PredicatePackageBinding,
    VerificationBasis,
    check_mission_evidence_readiness,
)
from scripts.smoke_px4_gazebo_sitl_e2e_delivery import (
    _completion_projection,
    _with_completion_projection,
)
from src.runtime.px4_gazebo_delivery_predicate_package import (
    PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_ID,
    PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SHA256,
    PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION,
    PX4GazeboDeliveryEvidenceBindings,
    PX4GazeboDeliveryPredicateContent,
    PX4GazeboDeliveryPredicateStatus,
    PX4GazeboDeliveryReplayInput,
    build_px4_gazebo_delivery_replay_contract,
    build_px4_gazebo_delivery_replay_input,
    evaluate_px4_gazebo_delivery_predicate,
)
from src.runtime.px4_gazebo_sitl_e2e_delivery_smoke import (
    build_px4_gazebo_sitl_e2e_delivery_epic_exit_result,
    build_px4_gazebo_sitl_e2e_delivery_smoke_result,
)


pytestmark = pytest.mark.contract

OBSERVED_AT = datetime(2026, 7, 29, 0, 0, 0, tzinfo=timezone.utc)
EVALUATED_AT = "2026-07-29T00:00:05+00:00"
RELEASE_REF = "px4_gazebo_sitl_payload_release_event:release-1"
DROPOFF_REF = "px4_gazebo_sitl_dropoff_verification:verification-1"


def _contract():
    return build_px4_gazebo_delivery_replay_contract(
        contract_id="px4-sim-delivery-contract-1",
        contract_version="2026-07-29",
        approved_drop_zone={
            "zone_id": "dropoff-pad-b",
            "radius_m": 1.0,
            "scope": "sim",
        },
        approved_payload_release_rule={
            "source": "gazebo_detachable_joint_detach_event",
            "release_window_seconds": 5.0,
        },
        approved_same_session_rule={
            "mission_upload_and_release_same_session": True,
        },
        maximum_observation_age_seconds=30.0,
    )


def _epic_exit_result():
    return build_px4_gazebo_sitl_e2e_delivery_epic_exit_result(
        prompt="deliver the prepared simulator payload",
        horizontal_summary={
            "payload_release_observed": True,
            "payload_release_event_source": (
                "gazebo_detachable_joint_detach_event"
            ),
            "preupload_mission_request_sequences": [0, 1, 2, 3],
            "climb_sample_count": 8,
            "dropoff_region_reached": True,
            "completed_pose_z_m": 0.1,
        },
        payload_release_event_ref=RELEASE_REF,
        dropoff_verification_ref=DROPOFF_REF,
        artifact_manifest={
            "horizontal_route_artifact_dir": "sanitized-run",
            "payload_release_event_ref": RELEASE_REF,
            "dropoff_verification_ref": DROPOFF_REF,
        },
        observed_at=OBSERVED_AT,
    )


def _content() -> PX4GazeboDeliveryPredicateContent:
    return PX4GazeboDeliveryPredicateContent.from_epic_exit_result(
        _epic_exit_result(),
        evidence_bindings=_evidence_bindings(),
    )


def _evidence_bindings() -> PX4GazeboDeliveryEvidenceBindings:
    return PX4GazeboDeliveryEvidenceBindings(
        payload_release_event_sha256="d" * 64,
        dropoff_verification_sha256="e" * 64,
        sitl_telemetry_log_sha256="f" * 64,
        gazebo_pose_trace_sha256="1" * 64,
        mission_artifacts_sha256="2" * 64,
    )


def _replay(contract=None, content=None) -> PX4GazeboDeliveryReplayInput:
    selected_contract = contract or _contract()
    return build_px4_gazebo_delivery_replay_input(
        contract=selected_contract,
        content=content or _content(),
    )


def test_production_result_shape_satisfies_only_the_scoped_sim_claim() -> None:
    contract = _contract()
    result = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=_replay(contract),
        evaluated_at=EVALUATED_AT,
    )

    assert result.status is PX4GazeboDeliveryPredicateStatus.SATISFIED
    assert result.evaluated_outcome_claim is True
    assert result.actual_verification_basis is VerificationBasis.DETERMINISTIC
    assert result.outcome_claim_scope == "px4_gazebo_simulator_delivery"
    assert result.evaluated_at == EVALUATED_AT
    assert result.observation_content_sha256 == _content().content_sha256
    assert result.evidence_origins == (EvidenceOrigin.STORED_ARTIFACT,)
    assert result.evidence_refs == (
        f"mission-observation:px4-gazebo-delivery-result:{_content().source_result_id}",
        RELEASE_REF,
        DROPOFF_REF,
    )
    assert result.reasons == ()
    assert result.predicate_package_evaluated is True
    assert result.approval_created is False
    assert result.dispatch_authority_created is False
    assert result.runtime_effect_requested is False
    assert result.operational_closure_created is False
    assert result.physical_execution_invoked is False


def test_runtime_completion_is_projected_from_predicate_result() -> None:
    contract = _contract()
    evaluation = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=_replay(contract),
        evaluated_at=EVALUATED_AT,
    )

    projection = _completion_projection(
        legacy_epic_exit_complete=True,
        predicate_evaluation=evaluation,
    )

    assert projection == {
        "legacy_epic_exit_complete": True,
        "completion_claimed": True,
        "completion_scope": "px4_gazebo_simulator_delivery",
    }


def test_legacy_completion_does_not_override_predicate_failure() -> None:
    content = replace(_content(), executed_in_same_sitl_session=False)
    contract = _contract()
    evaluation = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=_replay(contract, content),
        evaluated_at=EVALUATED_AT,
    )

    summary = _with_completion_projection(
        {"epic_exit_complete": True},
        legacy_epic_exit_complete=True,
        predicate_evaluation=evaluation,
    )

    assert summary == {
        "epic_exit_complete": True,
        "legacy_epic_exit_complete": True,
        "completion_claimed": False,
        "completion_scope": "none",
    }


def test_summary_rejects_preexisting_completion_projection_keys() -> None:
    contract = _contract()
    evaluation = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=_replay(contract),
        evaluated_at=EVALUATED_AT,
    )

    with pytest.raises(
        ValueError,
        match="completion projection keys already present: completion_claimed",
    ):
        _with_completion_projection(
            {"completion_claimed": True},
            legacy_epic_exit_complete=True,
            predicate_evaluation=evaluation,
        )


@pytest.mark.parametrize(
    ("content_change", "expected_reason"),
    [
        (
            {"source_schema_version": "unknown-result.v1"},
            "source_result_schema_mismatch",
        ),
        ({"source_result_id": ""}, "source_result_id_missing"),
        (
            {"executed_in_same_sitl_session": False},
            "same_session_execution_not_observed",
        ),
        ({"mission_upload_observed": False}, "mission_upload_not_observed"),
        ({"mission_ack_observed": False}, "mission_ack_not_observed"),
        ({"mission_ack_type": 1}, "mission_ack_type_not_accepted"),
        (
            {"mission_request_sequences": (0, 1, 2)},
            "mission_request_sequences_incomplete",
        ),
        ({"actual_takeoff_observed": False}, "takeoff_not_observed"),
        (
            {"actual_dropoff_region_reached": False},
            "approved_drop_zone_not_reached",
        ),
        ({"actual_land_observed": False}, "landing_not_observed"),
        ({"payload_release_observed": False}, "payload_release_not_observed"),
        ({"payload_release_verified": False}, "payload_release_not_verified"),
        (
            {"payload_release_event_source": "declared_release"},
            "payload_release_source_mismatch",
        ),
        (
            {"gazebo_detachable_joint_release_observed": False},
            "detachable_joint_release_not_observed",
        ),
        ({"payload_release_event_ref": ""}, "payload_release_event_ref_invalid"),
        ({"dropoff_verification_ref": ""}, "dropoff_verification_ref_invalid"),
        (
            {"artifact_payload_release_event_ref": "different-ref"},
            "artifact_payload_release_event_ref_mismatch",
        ),
        (
            {"artifact_dropoff_verification_ref": "different-ref"},
            "artifact_dropoff_verification_ref_mismatch",
        ),
        (
            {"external_dispatch_scope": "same_session_sitl_mission_upload"},
            "external_dispatch_scope_mismatch",
        ),
        ({"blocked_reasons": ("blocked",)}, "blocked_reasons_present"),
        (
            {"physical_execution_invoked": True},
            "physical_execution_claim_forbidden",
        ),
        ({"artifact_manifest_sha256": ""}, "artifact_manifest_digest_invalid"),
    ],
)
def test_each_required_fact_is_fail_closed(
    content_change: dict[str, object],
    expected_reason: str,
) -> None:
    contract = _contract()
    content = replace(_content(), **content_change)

    result = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=_replay(contract, content),
        evaluated_at=EVALUATED_AT,
    )

    assert result.status is PX4GazeboDeliveryPredicateStatus.NOT_SATISFIED
    assert result.evaluated_outcome_claim is False
    assert result.actual_verification_basis is VerificationBasis.DETERMINISTIC
    assert expected_reason in result.reasons
    assert result.dispatch_authority_created is False
    assert result.runtime_effect_requested is False


@pytest.mark.parametrize(
    ("binding_field", "expected_reason"),
    [
        (
            "payload_release_event_sha256",
            "payload_release_event_content_digest_invalid",
        ),
        (
            "dropoff_verification_sha256",
            "dropoff_verification_content_digest_invalid",
        ),
        ("sitl_telemetry_log_sha256", "sitl_telemetry_log_digest_invalid"),
        ("gazebo_pose_trace_sha256", "gazebo_pose_trace_digest_invalid"),
        ("mission_artifacts_sha256", "mission_artifacts_digest_invalid"),
    ],
)
def test_each_source_artifact_requires_a_content_digest(
    binding_field: str,
    expected_reason: str,
) -> None:
    contract = _contract()
    bindings = replace(_evidence_bindings(), **{binding_field: ""})
    content = replace(_content(), evidence_bindings=bindings)

    result = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=_replay(contract, content),
        evaluated_at=EVALUATED_AT,
    )

    assert result.status is PX4GazeboDeliveryPredicateStatus.NOT_SATISFIED
    assert result.evaluated_outcome_claim is False
    assert expected_reason in result.reasons


@pytest.mark.parametrize(
    "binding",
    [
        PredicatePackageBinding(
            package_id="another-package",
            package_version=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION,
            content_sha256=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SHA256,
        ),
        PredicatePackageBinding(
            package_id=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_ID,
            package_version="2",
            content_sha256=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_SHA256,
        ),
        PredicatePackageBinding(
            package_id=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_ID,
            package_version=PX4_GAZEBO_DELIVERY_PREDICATE_PACKAGE_VERSION,
            content_sha256="d" * 64,
        ),
    ],
)
def test_package_binding_mismatch_is_blocked(
    binding: PredicatePackageBinding,
) -> None:
    contract = replace(_contract(), predicate_package=binding)

    result = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=_replay(contract),
        evaluated_at=EVALUATED_AT,
    )

    assert result.status is PX4GazeboDeliveryPredicateStatus.BLOCKED
    assert result.evaluated_outcome_claim is False
    assert result.actual_verification_basis is VerificationBasis.UNVERIFIED
    assert "predicate_package_binding_mismatch" in result.reasons
    assert result.predicate_package_evaluated is False


def test_content_digest_mismatch_is_blocked() -> None:
    contract = _contract()
    replay = _replay(contract)
    changed_content = replace(
        replay.content,
        actual_dropoff_region_reached=False,
    )

    result = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=replace(replay, content=changed_content),
        evaluated_at=EVALUATED_AT,
    )

    assert result.status is PX4GazeboDeliveryPredicateStatus.BLOCKED
    assert "predicate_observation_content_binding_mismatch" in result.reasons
    assert result.predicate_package_evaluated is False


def test_stale_observation_does_not_reach_the_package() -> None:
    contract = _contract()

    result = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=_replay(contract),
        evaluated_at="2026-07-29T00:01:00+00:00",
    )

    assert result.status is PX4GazeboDeliveryPredicateStatus.UNVERIFIED
    assert "observation_stale:px4_gazebo_delivery_result" in result.reasons
    assert result.predicate_package_evaluated is False


@pytest.mark.parametrize(
    ("observation_change", "expected_reason"),
    [
        (
            {"execution_scope": HardwareExecutionMode.FIELD},
            "observation_execution_scope_mismatch:px4_gazebo_delivery_result",
        ),
        (
            {"origin": EvidenceOrigin.OPERATOR_DECLARED},
            "observation_origin_mismatch:px4_gazebo_delivery_result",
        ),
    ],
)
def test_wrong_scope_or_origin_does_not_reach_the_package(
    observation_change: dict[str, object],
    expected_reason: str,
) -> None:
    contract = _contract()
    replay = _replay(contract)
    observation = replace(
        replay.evidence.observations[0],
        **observation_change,
    )
    evidence = replace(replay.evidence, observations=(observation,))

    result = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=replace(replay, evidence=evidence),
        evaluated_at=EVALUATED_AT,
    )

    assert result.status is PX4GazeboDeliveryPredicateStatus.UNVERIFIED
    assert expected_reason in result.reasons
    assert result.predicate_package_evaluated is False


def test_stored_artifact_origin_does_not_itself_produce_a_basis() -> None:
    contract = _contract()
    replay = _replay(contract)

    readiness = check_mission_evidence_readiness(
        contract=contract,
        evidence=replay.evidence,
        evaluated_at=EVALUATED_AT,
    )
    evaluated = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=replay,
        evaluated_at=EVALUATED_AT,
    )

    assert readiness.evidence_readiness is MissionEvidenceReadiness.READY
    assert readiness.actual_verification_basis is VerificationBasis.UNVERIFIED
    assert readiness.evaluated_outcome_claim is False
    assert evaluated.actual_verification_basis is VerificationBasis.DETERMINISTIC
    assert evaluated.predicate_package_evaluated is True


def test_flight_only_result_cannot_claim_delivery_completion() -> None:
    flight_only = build_px4_gazebo_sitl_e2e_delivery_smoke_result(
        prompt="deliver the prepared simulator payload",
        horizontal_summary={
            "preupload_mission_request_sequences": [0, 1, 2, 3],
            "climb_sample_count": 8,
            "dropoff_region_reached": True,
            "completed_pose_z_m": 0.1,
        },
        artifact_manifest={"horizontal_route_artifact_dir": "sanitized-run"},
        observed_at=OBSERVED_AT,
    )
    content = PX4GazeboDeliveryPredicateContent.from_flight_only_result(
        flight_only,
        evidence_bindings=_evidence_bindings(),
    )
    contract = _contract()

    result = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=_replay(contract, content),
        evaluated_at=EVALUATED_AT,
    )

    assert result.status is PX4GazeboDeliveryPredicateStatus.NOT_SATISFIED
    assert result.evaluated_outcome_claim is False
    assert {
        "payload_release_not_observed",
        "payload_release_not_verified",
        "detachable_joint_release_not_observed",
        "payload_release_event_ref_invalid",
        "dropoff_verification_ref_invalid",
        "external_dispatch_scope_mismatch",
        "blocked_reasons_present",
    }.issubset(result.reasons)


def test_multiple_runtime_observations_are_not_silently_collapsed() -> None:
    contract = _contract()
    replay = _replay(contract)
    evidence = MissionRuntimeEvidence(
        contract_sha256=contract.contract_sha256,
        observations=(
            replay.evidence.observations[0],
            replace(
                replay.evidence.observations[0],
                observation_id="second-observation",
            ),
        ),
    )

    result = evaluate_px4_gazebo_delivery_predicate(
        contract=contract,
        replay=replace(replay, evidence=evidence),
        evaluated_at=EVALUATED_AT,
    )

    assert result.status is PX4GazeboDeliveryPredicateStatus.BLOCKED
    assert "predicate_observation_content_binding_mismatch" in result.reasons
    assert result.predicate_package_evaluated is False
