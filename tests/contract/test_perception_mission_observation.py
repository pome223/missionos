from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

from missionos_core import (
    EvidenceOrigin,
    FrozenMissionContract,
    HardwareExecutionMode,
    MissionEvidenceReadiness,
    MissionRuntimeEvidence,
    ObservationFreshnessBasis,
    ObservationRequirement,
    OutcomeClaimSpec,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    TerminationPolicy,
    TerminationReason,
    UTC_WALL_CLOCK_DOMAIN_REF,
    VerificationBasis,
    check_mission_evidence_readiness,
)
from src.runtime.perception_claim import (
    build_perception_claim_from_camera_observation,
)
from src.runtime.perception_mission_observation import (
    PERCEPTION_MISSION_OBSERVATION_EVIDENCE_KIND,
    project_perception_claim_to_mission_observation,
)


SOURCE_TIME = datetime(1970, 1, 1, 0, 0, 50, 128000, tzinfo=timezone.utc)
RECEIVED_TIME = datetime(2026, 7, 21, 8, 27, 26, 259169, tzinfo=timezone.utc)
EVALUATED_TIME = datetime(2026, 7, 21, 8, 27, 53, 92480, tzinfo=timezone.utc)
IMAGE_SHA256 = "a" * 64
FRAME_REF = f"sha256:{IMAGE_SHA256}"
SOURCE_CLOCK = "clock:sim:episode-test"


def _runtime_context() -> dict[str, object]:
    stdout = '{"claim_kind":"corridor_blocked_by_object"}'
    return {
        "decision_epoch_ref": "epoch:test",
        "capture": {
            "camera_frame_sha256": IMAGE_SHA256,
            "camera_lidar_observation": {
                "camera_observed_at": SOURCE_TIME.isoformat(),
                "camera_received_at": RECEIVED_TIME.isoformat(),
                "camera_width": 640,
                "camera_fx": 554.25,
                "camera_cx": 320.0,
                "lidar_observed_at": (
                    SOURCE_TIME - timedelta(milliseconds=128)
                ).isoformat(),
                "lidar_obstacle_observed": True,
                "lidar_horizontal_sector": "center",
                "lidar_candidate_bearing_rad": 0.0,
                "target_candidate_id": "lidar_candidate:test",
                "lidar_evidence_ref": "laser_scan:test",
            },
        },
        "llm_invocation_evidence": {
            "schema_version": "runtime_invocation_evidence.v1",
            "invocation_kind": "llm_api",
            "invocation_target": "google_adk:gemini-test",
            "provider": "google_adk_gemini",
            "model_id": "gemini-test",
            "input_image_sha256": IMAGE_SHA256,
            "prompt_sha256": sha256(b"prompt").hexdigest(),
            "invocation_started_at": EVALUATED_TIME.isoformat(),
            "invocation_completed_at": (
                EVALUATED_TIME + timedelta(milliseconds=100)
            ).isoformat(),
            "invocation_stdout_sha256": sha256(stdout.encode()).hexdigest(),
            "invocation_stderr_sha256": sha256(b"").hexdigest(),
            "invocation_stdout_preimage": stdout,
            "invocation_stderr_preimage": "",
            "invocation_exit_code": 0,
            "invocation_ref": "vlm_invocation:test",
            "physical_execution_invoked": False,
        },
    }


def _claim():
    claim = build_perception_claim_from_camera_observation(
        {
            "claim_kind": "corridor_blocked_by_object",
            "source_frame_ref": FRAME_REF,
            "confidence": 0.9,
            "horizontal_sector": "center",
            "target_center_x_normalized": 0.5,
        },
        costmap_obstacle_observed=False,
        runtime_context=_runtime_context(),
    )
    assert claim is not None
    return claim


def _contract() -> FrozenMissionContract:
    return FrozenMissionContract(
        contract_id="perception-contract-test",
        contract_version="1",
        execution_scope=HardwareExecutionMode.SIM,
        reference_inputs=(),
        observation_requirements=(
            ObservationRequirement(
                requirement_id="visual_claim",
                evidence_kind=PERCEPTION_MISSION_OBSERVATION_EVIDENCE_KIND,
                required_origin=EvidenceOrigin.MODEL_INFERRED,
                maximum_age_seconds=120.0,
                freshness_basis=(
                    ObservationFreshnessBasis.MISSION_RECEIVED_AT
                ),
                source_clock_domain_ref=SOURCE_CLOCK,
                receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                require_source_receipt_binding=True,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="one model-inferred visual claim",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id="visual-claim-ready",
            statement="The visual claim is ready for a later predicate.",
            claim_scope="one visual claim",
        ),
        predicate_package=PredicatePackageBinding(
            package_id="not-evaluated-here",
            package_version="1",
            content_sha256="b" * 64,
        ),
        termination_policy=TerminationPolicy(
            allowed_reasons=(TerminationReason.EXPIRY,)
        ),
        required_verification_basis=VerificationBasis.MODEL_INFERRED,
    )


def test_projection_preserves_dual_clock_times_and_model_origin() -> None:
    result = project_perception_claim_to_mission_observation(
        claim=_claim(),
        requirement_id="visual_claim",
        execution_scope=HardwareExecutionMode.SIM,
        source_clock_domain_ref=SOURCE_CLOCK,
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
    )

    assert result.status == "projected"
    assert result.observation is not None
    assert result.observation.origin is EvidenceOrigin.MODEL_INFERRED
    assert result.observation.observed_at.startswith("1970-")
    assert result.observation.received_at is not None
    assert result.observation.received_at.startswith("2026-")
    assert result.observation.source_receipt_binding_sha256 is not None
    assert result.dispatch_authority_created is False
    assert result.physical_execution_invoked is False


def test_receipt_freshness_replay_is_ready_without_promoting_claim() -> None:
    contract = _contract()
    projection = project_perception_claim_to_mission_observation(
        claim=_claim(),
        requirement_id="visual_claim",
        execution_scope=HardwareExecutionMode.SIM,
        source_clock_domain_ref=SOURCE_CLOCK,
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
    )
    assert projection.observation is not None

    result = check_mission_evidence_readiness(
        contract=contract,
        evidence=MissionRuntimeEvidence(
            contract_sha256=contract.contract_sha256,
            observations=(projection.observation,),
        ),
        evaluated_at=EVALUATED_TIME.isoformat(),
    )

    assert result.evidence_readiness is MissionEvidenceReadiness.READY
    assert result.evaluated_outcome_claim is False
    assert result.actual_verification_basis is VerificationBasis.UNVERIFIED
    assert result.approval_created is False
    assert result.dispatch_authority_created is False


def test_source_freshness_does_not_compare_sim_time_to_wall_clock() -> None:
    contract = _contract()
    requirement = contract.observation_requirements[0]
    contract = FrozenMissionContract(
        **{
            **contract.__dict__,
            "observation_requirements": (
                ObservationRequirement(
                    requirement_id=requirement.requirement_id,
                    evidence_kind=requirement.evidence_kind,
                    required_origin=requirement.required_origin,
                    maximum_age_seconds=requirement.maximum_age_seconds,
                    freshness_basis=(
                        ObservationFreshnessBasis.SOURCE_OBSERVED_AT
                    ),
                    source_clock_domain_ref=SOURCE_CLOCK,
                ),
            ),
        }
    )
    projection = project_perception_claim_to_mission_observation(
        claim=_claim(),
        requirement_id="visual_claim",
        execution_scope=HardwareExecutionMode.SIM,
        source_clock_domain_ref=SOURCE_CLOCK,
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
    )
    assert projection.observation is not None

    result = check_mission_evidence_readiness(
        contract=contract,
        evidence=MissionRuntimeEvidence(
            contract_sha256=contract.contract_sha256,
            observations=(projection.observation,),
        ),
        evaluated_at=EVALUATED_TIME.isoformat(),
    )

    assert result.evidence_readiness is MissionEvidenceReadiness.INCOMPLETE
    assert "observation_stale:visual_claim" not in result.reasons
    assert (
        "observation_freshness_clock_domain_mismatch:visual_claim"
        in result.reasons
    )


def test_unbound_claim_is_refused_instead_of_getting_receipt_freshness() -> None:
    claim = _claim().model_copy(update={"corroboration_binding": None})

    result = project_perception_claim_to_mission_observation(
        claim=claim,
        requirement_id="visual_claim",
        execution_scope=HardwareExecutionMode.SIM,
        source_clock_domain_ref=SOURCE_CLOCK,
        receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
    )

    assert result.status == "refused"
    assert result.observation is None
    assert "perception_mission_observation_binding_missing" in result.reasons
