"""Concrete Nav2/TurtleBot3 bounded-simulator-action predicate package.

This module evaluates one content-bound Nav2 result. It does not define a
general predicate language and does not turn MissionOS into a Nav2 controller.
Predicate satisfaction updates verifier state only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from missionos_core import (
    EvidenceOrigin,
    FrozenMissionContract,
    HardwareExecutionMode,
    MissionEvidenceReadiness,
    MissionObservation,
    MissionRuntimeEvidence,
    ObservationRequirement,
    OutcomeClaimSpec,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    ReferenceInput,
    TerminationPolicy,
    TerminationReason,
    VerificationBasis,
    canonical_sha256,
    check_mission_evidence_readiness,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from .hardware_adapter_contract import HardwareAdapterEvidence


NAV2_TURTLEBOT3_RESULT_SCHEMA_VERSION = (
    "missionos_nav2_turtlebot3_bounded_dispatch_result.v1"
)
NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SCHEMA_VERSION = (
    "missionos_nav2_turtlebot3_predicate_package.v1"
)
NAV2_TURTLEBOT3_PREDICATE_PACKAGE_ID = "nav2_sim_action_completed"
NAV2_TURTLEBOT3_PREDICATE_PACKAGE_VERSION = "1"
NAV2_TURTLEBOT3_OUTCOME_CLAIM_ID = "nav2_sim_action_completed"
NAV2_TURTLEBOT3_OUTCOME_CLAIM_SCOPE = "nav2_turtlebot3_sim_action"
NAV2_TURTLEBOT3_OBSERVATION_REQUIREMENT_ID = "nav2_bounded_dispatch_result"
NAV2_TURTLEBOT3_EVIDENCE_KIND = "nav2_turtlebot3_bounded_dispatch_result"

_PACKAGE_MATERIAL = {
    "schema_version": NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SCHEMA_VERSION,
    "package_id": NAV2_TURTLEBOT3_PREDICATE_PACKAGE_ID,
    "package_version": NAV2_TURTLEBOT3_PREDICATE_PACKAGE_VERSION,
    "outcome_claim_id": NAV2_TURTLEBOT3_OUTCOME_CLAIM_ID,
    "outcome_claim_scope": NAV2_TURTLEBOT3_OUTCOME_CLAIM_SCOPE,
    "reference_inputs": ["approved_goal_pose", "approved_goal_frame"],
    "source_boundary": {
        "bridge_action": "send_goal_pose",
        "adapter_id": "ros2_nav2_ground_robot_adapter.v1",
        "adapter_kind": "ros2_nav2",
        "adapter_action_kind": "nav2_goal_pose",
        "execution_mode": "sim",
    },
    "alternatives": {
        "succeeded_with_motion": [
            "accepted_goal",
            "nav2_goal_succeeded",
            "runtime_progress_observed",
            "odometry_motion_observed",
        ],
        "already_at_goal": [
            "accepted_goal",
            "nav2_goal_succeeded",
            "state_already_at_goal_pose",
            "progress_already_at_goal_pose",
        ],
    },
    "explicitly_excluded": ["position_tolerance_with_confirmed_cancel"],
    "requested_runtime_effect": "none",
}
NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SHA256 = canonical_sha256(_PACKAGE_MATERIAL)


class Nav2PredicateStatus(str, Enum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"


class Nav2StateResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    nav2_action_server_available: StrictBool = False
    nav2_goal_succeeded: StrictBool = False
    pose_observed: StrictBool = False
    robot_motion_observed: StrictBool = False
    odom_before_observed: StrictBool = False
    odom_after_observed: StrictBool = False
    odom_delta_m: StrictFloat = 0.0
    completion_basis: StrictStr | None = None
    goal_already_satisfied_observed: StrictBool = False


class Nav2ProgressResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    runtime_progress_observed: StrictBool = False
    completion_observed: StrictBool = False
    nav2_goal_succeeded: StrictBool = False
    nav2_status: StrictStr = ""
    robot_motion_observed: StrictBool = False
    completion_basis: StrictStr | None = None
    goal_already_satisfied_observed: StrictBool = False
    feedback_count: StrictInt = 0


class Nav2BridgeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: StrictStr = ""
    ack_status: StrictStr
    ack_source: StrictStr | None = None
    goal_accepted: StrictBool = False
    nav2_status: StrictStr = ""
    nav2_goal_succeeded: StrictBool = False
    runtime_progress_observed: StrictBool = False
    completion_observed: StrictBool = False
    completion_basis: StrictStr | None = None
    blocking_reasons: tuple[StrictStr, ...] = ()
    physical_execution_invoked: StrictBool = False
    raw_velocity_invoked: StrictBool = False
    raw_velocity_published: StrictBool = False
    raw_ros_topic_published: StrictBool = False
    cmd_vel_published_by_missionos: StrictBool = False
    state_result: Nav2StateResult
    progress_result: Nav2ProgressResult


class Nav2RequestedGoalPose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_id: StrictStr
    x_m: StrictFloat
    y_m: StrictFloat
    yaw_rad: StrictFloat
    tolerance_m: StrictFloat
    max_speed_mps: StrictFloat
    max_distance_m: StrictFloat
    label: StrictStr


class Nav2TurtleBot3BoundedDispatchResult(BaseModel):
    """Typed source saved and read back by the opt-in runtime smoke."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "missionos_nav2_turtlebot3_bounded_dispatch_result.v1"
    ] = NAV2_TURTLEBOT3_RESULT_SCHEMA_VERSION
    result_id: str
    observed_at: datetime
    requested_goal_pose: Nav2RequestedGoalPose
    bridge_response: Nav2BridgeResponse
    adapter_evidence: HardwareAdapterEvidence

    @model_validator(mode="before")
    @classmethod
    def _reject_coerced_adapter_facts(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        adapter = value.get("adapter_evidence")
        if not isinstance(adapter, dict):
            return value
        for field in (
            "dispatch_request_sent",
            "command_ack_observed",
            "runtime_progress_observed",
            "completion_claimed",
            "physical_execution_invoked",
        ):
            if not isinstance(adapter.get(field), bool):
                raise ValueError(f"adapter_evidence_{field}_invalid")
        return value


@dataclass(frozen=True)
class Nav2TurtleBot3EvidenceBindings:
    bridge_response_sha256: str
    adapter_evidence_sha256: str

    def to_material(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Nav2TurtleBot3PredicateContent:
    source_schema_version: str
    source_result_id: str
    observed_at: str
    requested_goal_pose_sha256: str
    requested_goal_frame_sha256: str
    bridge_action: str
    ack_status: str
    goal_accepted: bool
    nav2_status: str
    nav2_goal_succeeded: bool
    runtime_progress_observed: bool
    completion_observed: bool
    completion_basis: str | None
    state_nav2_goal_succeeded: bool
    state_robot_motion_observed: bool
    state_odom_before_observed: bool
    state_odom_after_observed: bool
    state_odom_delta_m: float
    state_completion_basis: str | None
    state_goal_already_satisfied_observed: bool
    progress_nav2_goal_succeeded: bool
    progress_runtime_progress_observed: bool
    progress_completion_observed: bool
    progress_robot_motion_observed: bool
    progress_completion_basis: str | None
    progress_goal_already_satisfied_observed: bool
    adapter_id: str
    adapter_kind: str
    adapter_action_kind: str
    adapter_execution_mode: str
    adapter_dispatch_request_sent: bool
    adapter_command_ack_observed: bool
    adapter_ack_status: str
    adapter_completion_claimed: bool
    adapter_completion_scope: str
    adapter_blocking_reasons: tuple[str, ...]
    physical_execution_invoked: bool
    forbidden_velocity_or_topic_claimed: bool
    evidence_bindings: Nav2TurtleBot3EvidenceBindings

    @classmethod
    def from_result(
        cls,
        result: Nav2TurtleBot3BoundedDispatchResult,
        *,
        evidence_bindings: Nav2TurtleBot3EvidenceBindings,
    ) -> Nav2TurtleBot3PredicateContent:
        bridge = result.bridge_response
        state = bridge.state_result
        progress = bridge.progress_result
        return cls(
            source_schema_version=result.schema_version,
            source_result_id=result.result_id,
            observed_at=result.observed_at.isoformat(),
            requested_goal_pose_sha256=canonical_sha256(
                result.requested_goal_pose.model_dump(mode="json")
            ),
            requested_goal_frame_sha256=canonical_sha256(
                {"frame_id": result.requested_goal_pose.frame_id}
            ),
            bridge_action=bridge.action,
            ack_status=bridge.ack_status,
            goal_accepted=bridge.goal_accepted,
            nav2_status=bridge.nav2_status,
            nav2_goal_succeeded=bridge.nav2_goal_succeeded,
            runtime_progress_observed=bridge.runtime_progress_observed,
            completion_observed=bridge.completion_observed,
            completion_basis=bridge.completion_basis,
            state_nav2_goal_succeeded=state.nav2_goal_succeeded,
            state_robot_motion_observed=state.robot_motion_observed,
            state_odom_before_observed=state.odom_before_observed,
            state_odom_after_observed=state.odom_after_observed,
            state_odom_delta_m=state.odom_delta_m,
            state_completion_basis=state.completion_basis,
            state_goal_already_satisfied_observed=(
                state.goal_already_satisfied_observed
            ),
            progress_nav2_goal_succeeded=progress.nav2_goal_succeeded,
            progress_runtime_progress_observed=(
                progress.runtime_progress_observed
            ),
            progress_completion_observed=progress.completion_observed,
            progress_robot_motion_observed=progress.robot_motion_observed,
            progress_completion_basis=progress.completion_basis,
            progress_goal_already_satisfied_observed=(
                progress.goal_already_satisfied_observed
            ),
            adapter_id=result.adapter_evidence.adapter_id,
            adapter_kind=result.adapter_evidence.adapter_kind.value,
            adapter_action_kind=(
                result.adapter_evidence.adapter_action_kind.value
            ),
            adapter_execution_mode=(
                result.adapter_evidence.execution_mode.value
            ),
            adapter_dispatch_request_sent=(
                result.adapter_evidence.dispatch_request_sent
            ),
            adapter_command_ack_observed=(
                result.adapter_evidence.command_ack_observed
            ),
            adapter_ack_status=result.adapter_evidence.ack_status.value,
            adapter_completion_claimed=(
                result.adapter_evidence.completion_claimed
            ),
            adapter_completion_scope=result.adapter_evidence.completion_scope,
            adapter_blocking_reasons=tuple(
                result.adapter_evidence.blocking_reasons
            ),
            physical_execution_invoked=(
                bridge.physical_execution_invoked
                or result.adapter_evidence.physical_execution_invoked
            ),
            forbidden_velocity_or_topic_claimed=any(
                (
                    bridge.raw_velocity_invoked,
                    bridge.raw_velocity_published,
                    bridge.raw_ros_topic_published,
                    bridge.cmd_vel_published_by_missionos,
                )
            ),
            evidence_bindings=evidence_bindings,
        )

    def to_material(self) -> dict[str, Any]:
        material = asdict(self)
        material["evidence_bindings"] = self.evidence_bindings.to_material()
        return material

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.to_material())


@dataclass(frozen=True)
class Nav2TurtleBot3ReplayInput:
    content: Nav2TurtleBot3PredicateContent
    evidence: MissionRuntimeEvidence


@dataclass(frozen=True)
class Nav2TurtleBot3PredicateEvaluation:
    contract_id: str
    contract_sha256: str
    predicate_package_id: str
    predicate_package_version: str
    predicate_package_sha256: str
    outcome_claim_id: str
    outcome_claim_scope: str
    satisfied_alternative: str | None
    evaluated_at: str
    observation_content_sha256: str
    evidence_readiness: MissionEvidenceReadiness
    status: Nav2PredicateStatus
    evaluated_outcome_claim: bool
    actual_verification_basis: VerificationBasis
    evidence_origins: tuple[EvidenceOrigin, ...]
    reasons: tuple[str, ...]
    predicate_package_evaluated: bool
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    operational_closure_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "evidence_readiness": self.evidence_readiness.value,
            "status": self.status.value,
            "actual_verification_basis": self.actual_verification_basis.value,
            "evidence_origins": [
                origin.value for origin in self.evidence_origins
            ],
        }


def nav2_turtlebot3_predicate_package_binding() -> PredicatePackageBinding:
    return PredicatePackageBinding(
        package_id=NAV2_TURTLEBOT3_PREDICATE_PACKAGE_ID,
        package_version=NAV2_TURTLEBOT3_PREDICATE_PACKAGE_VERSION,
        content_sha256=NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SHA256,
    )


def build_nav2_turtlebot3_replay_contract(
    *,
    contract_id: str,
    contract_version: str,
    approved_goal_pose: dict[str, Any],
    approved_goal_frame: dict[str, Any],
    maximum_observation_age_seconds: float,
) -> FrozenMissionContract:
    return FrozenMissionContract(
        contract_id=contract_id,
        contract_version=contract_version,
        execution_scope=HardwareExecutionMode.SIM,
        reference_inputs=(
            ReferenceInput(
                input_id="approved_goal_pose",
                kind="approved_navigation_goal",
                content_sha256=canonical_sha256(approved_goal_pose),
            ),
            ReferenceInput(
                input_id="approved_goal_frame",
                kind="approved_coordinate_frame",
                content_sha256=canonical_sha256(approved_goal_frame),
            ),
        ),
        observation_requirements=(
            ObservationRequirement(
                requirement_id=NAV2_TURTLEBOT3_OBSERVATION_REQUIREMENT_ID,
                evidence_kind=NAV2_TURTLEBOT3_EVIDENCE_KIND,
                required_origin=EvidenceOrigin.STORED_ARTIFACT,
                maximum_age_seconds=maximum_observation_age_seconds,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason="The claim concerns one frozen bounded goal.",
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id=NAV2_TURTLEBOT3_OUTCOME_CLAIM_ID,
            statement="The frozen Nav2 simulator action completed.",
            claim_scope=NAV2_TURTLEBOT3_OUTCOME_CLAIM_SCOPE,
        ),
        predicate_package=nav2_turtlebot3_predicate_package_binding(),
        termination_policy=TerminationPolicy(
            allowed_reasons=(
                TerminationReason.EXPIRY,
                TerminationReason.OPERATOR_INTERRUPTION,
                TerminationReason.SAFE_STOP,
                TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            ),
        ),
        required_verification_basis=VerificationBasis.DETERMINISTIC,
    )


def build_nav2_turtlebot3_replay_input(
    *,
    contract: FrozenMissionContract,
    content: Nav2TurtleBot3PredicateContent,
) -> Nav2TurtleBot3ReplayInput:
    observation = MissionObservation(
        observation_id=f"nav2-bounded-dispatch:{content.source_result_id}",
        requirement_id=NAV2_TURTLEBOT3_OBSERVATION_REQUIREMENT_ID,
        evidence_kind=NAV2_TURTLEBOT3_EVIDENCE_KIND,
        origin=EvidenceOrigin.STORED_ARTIFACT,
        observed_at=content.observed_at,
        content_sha256=content.content_sha256,
        execution_scope=HardwareExecutionMode.SIM,
    )
    return Nav2TurtleBot3ReplayInput(
        content=content,
        evidence=MissionRuntimeEvidence(
            contract_sha256=contract.contract_sha256,
            observations=(observation,),
        ),
    )


def evaluate_nav2_turtlebot3_predicate(
    *,
    contract: FrozenMissionContract,
    replay: Nav2TurtleBot3ReplayInput,
    evaluated_at: str,
) -> Nav2TurtleBot3PredicateEvaluation:
    readiness = check_mission_evidence_readiness(
        contract=contract,
        evidence=replay.evidence,
        evaluated_at=evaluated_at,
    )
    observation = (
        replay.evidence.observations[0]
        if len(replay.evidence.observations) == 1
        else None
    )
    boundary_reasons: list[str] = []
    if (
        contract.predicate_package
        != nav2_turtlebot3_predicate_package_binding()
        or contract.outcome_claim_spec.claim_id
        != NAV2_TURTLEBOT3_OUTCOME_CLAIM_ID
        or contract.outcome_claim_spec.claim_scope
        != NAV2_TURTLEBOT3_OUTCOME_CLAIM_SCOPE
    ):
        boundary_reasons.append("predicate_package_binding_mismatch")
    reference_inputs = {
        item.input_id: item.content_sha256
        for item in contract.reference_inputs
    }
    if (
        reference_inputs.get("approved_goal_pose")
        != replay.content.requested_goal_pose_sha256
        or reference_inputs.get("approved_goal_frame")
        != replay.content.requested_goal_frame_sha256
    ):
        boundary_reasons.append("approved_goal_binding_mismatch")
    if (
        observation is None
        or observation.content_sha256 != replay.content.content_sha256
        or observation.observed_at != replay.content.observed_at
    ):
        boundary_reasons.append("predicate_observation_content_binding_mismatch")

    if (
        readiness.evidence_readiness is not MissionEvidenceReadiness.READY
        or boundary_reasons
    ):
        reasons = tuple(
            reason
            for reason in readiness.reasons
            if reason != "outcome_claim_requires_approved_predicate_package"
        ) + tuple(boundary_reasons)
        return _evaluation(
            contract=contract,
            replay=replay,
            evaluated_at=evaluated_at,
            readiness=readiness.evidence_readiness,
            status=(
                Nav2PredicateStatus.BLOCKED
                if readiness.evidence_readiness
                is MissionEvidenceReadiness.REFUSED
                or boundary_reasons
                else Nav2PredicateStatus.UNVERIFIED
            ),
            evaluated_outcome_claim=False,
            actual_verification_basis=VerificationBasis.UNVERIFIED,
            reasons=reasons,
            satisfied_alternative=None,
            predicate_package_evaluated=False,
        )

    reasons, alternative = _predicate_result(replay.content)
    return _evaluation(
        contract=contract,
        replay=replay,
        evaluated_at=evaluated_at,
        readiness=MissionEvidenceReadiness.READY,
        status=(
            Nav2PredicateStatus.SATISFIED
            if alternative is not None
            else Nav2PredicateStatus.NOT_SATISFIED
        ),
        evaluated_outcome_claim=alternative is not None,
        actual_verification_basis=VerificationBasis.DETERMINISTIC,
        reasons=reasons,
        satisfied_alternative=alternative,
        predicate_package_evaluated=True,
    )


def _predicate_result(
    content: Nav2TurtleBot3PredicateContent,
) -> tuple[tuple[str, ...], str | None]:
    common = (
        content.source_schema_version == NAV2_TURTLEBOT3_RESULT_SCHEMA_VERSION
        and content.bridge_action == "send_goal_pose"
        and content.ack_status == "accepted"
        and content.goal_accepted
        and content.nav2_status == "succeeded"
        and content.nav2_goal_succeeded
        and content.runtime_progress_observed
        and content.completion_observed
        and content.state_nav2_goal_succeeded
        and content.progress_nav2_goal_succeeded
        and content.progress_runtime_progress_observed
        and content.progress_completion_observed
        and content.adapter_id == "ros2_nav2_ground_robot_adapter.v1"
        and content.adapter_kind == "ros2_nav2"
        and content.adapter_action_kind == "nav2_goal_pose"
        and content.adapter_execution_mode == "sim"
        and content.adapter_dispatch_request_sent
        and content.adapter_command_ack_observed
        and content.adapter_ack_status == "accepted"
        and content.adapter_completion_claimed
        and content.adapter_completion_scope == "sim_action"
        and not content.adapter_blocking_reasons
        and not content.physical_execution_invoked
        and not content.forbidden_velocity_or_topic_claimed
        and _sha256_is_valid(
            content.evidence_bindings.bridge_response_sha256
        )
        and _sha256_is_valid(
            content.evidence_bindings.adapter_evidence_sha256
        )
    )
    motion = (
        common
        and content.completion_basis == "nav2_goal_succeeded"
        and content.state_completion_basis == "nav2_goal_succeeded"
        and content.progress_completion_basis == "nav2_goal_succeeded"
        and content.state_robot_motion_observed
        and content.progress_robot_motion_observed
        and content.state_odom_before_observed
        and content.state_odom_after_observed
        and content.state_odom_delta_m > 0
    )
    already_at_goal = (
        common
        and content.completion_basis == "already_at_goal_pose"
        and content.state_completion_basis == "already_at_goal_pose"
        and content.progress_completion_basis == "already_at_goal_pose"
        and content.state_goal_already_satisfied_observed
        and content.progress_goal_already_satisfied_observed
    )
    if motion:
        return (), "succeeded_with_motion"
    if already_at_goal:
        return (), "already_at_goal"

    checks = {
        "source_schema_invalid": (
            content.source_schema_version
            == NAV2_TURTLEBOT3_RESULT_SCHEMA_VERSION
        ),
        "bridge_action_invalid": content.bridge_action == "send_goal_pose",
        "goal_not_accepted": (
            content.ack_status == "accepted" and content.goal_accepted
        ),
        "nav2_goal_result_not_succeeded": (
            content.nav2_status == "succeeded"
            and content.nav2_goal_succeeded
            and content.state_nav2_goal_succeeded
            and content.progress_nav2_goal_succeeded
        ),
        "runtime_progress_not_observed": (
            content.runtime_progress_observed
            and content.progress_runtime_progress_observed
        ),
        "completion_not_observed": (
            content.completion_observed
            and content.progress_completion_observed
        ),
        "adapter_identity_invalid": (
            content.adapter_id == "ros2_nav2_ground_robot_adapter.v1"
            and content.adapter_kind == "ros2_nav2"
            and content.adapter_action_kind == "nav2_goal_pose"
            and content.adapter_execution_mode == "sim"
        ),
        "adapter_dispatch_not_observed": (
            content.adapter_dispatch_request_sent
        ),
        "adapter_ack_not_observed": (
            content.adapter_command_ack_observed
            and content.adapter_ack_status == "accepted"
        ),
        "completion_alternative_not_satisfied": motion or already_at_goal,
        "adapter_completion_not_claimed": content.adapter_completion_claimed,
        "adapter_completion_scope_invalid": (
            content.adapter_completion_scope == "sim_action"
        ),
        "adapter_blocked": not content.adapter_blocking_reasons,
        "physical_execution_claim_invalid": (
            not content.physical_execution_invoked
        ),
        "forbidden_velocity_or_topic_claimed": (
            not content.forbidden_velocity_or_topic_claimed
        ),
        "bridge_response_digest_invalid": _sha256_is_valid(
            content.evidence_bindings.bridge_response_sha256
        ),
        "adapter_evidence_digest_invalid": _sha256_is_valid(
            content.evidence_bindings.adapter_evidence_sha256
        ),
    }
    return tuple(reason for reason, passed in checks.items() if not passed), None


def _evaluation(
    *,
    contract: FrozenMissionContract,
    replay: Nav2TurtleBot3ReplayInput,
    evaluated_at: str,
    readiness: MissionEvidenceReadiness,
    status: Nav2PredicateStatus,
    evaluated_outcome_claim: bool,
    actual_verification_basis: VerificationBasis,
    reasons: tuple[str, ...],
    satisfied_alternative: str | None,
    predicate_package_evaluated: bool,
) -> Nav2TurtleBot3PredicateEvaluation:
    return Nav2TurtleBot3PredicateEvaluation(
        contract_id=contract.contract_id,
        contract_sha256=contract.contract_sha256,
        predicate_package_id=NAV2_TURTLEBOT3_PREDICATE_PACKAGE_ID,
        predicate_package_version=NAV2_TURTLEBOT3_PREDICATE_PACKAGE_VERSION,
        predicate_package_sha256=NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SHA256,
        outcome_claim_id=NAV2_TURTLEBOT3_OUTCOME_CLAIM_ID,
        outcome_claim_scope=NAV2_TURTLEBOT3_OUTCOME_CLAIM_SCOPE,
        satisfied_alternative=satisfied_alternative,
        evaluated_at=evaluated_at,
        observation_content_sha256=replay.content.content_sha256,
        evidence_readiness=readiness,
        status=status,
        evaluated_outcome_claim=evaluated_outcome_claim,
        actual_verification_basis=actual_verification_basis,
        evidence_origins=tuple(
            observation.origin for observation in replay.evidence.observations
        ),
        reasons=tuple(dict.fromkeys(reasons)),
        predicate_package_evaluated=predicate_package_evaluated,
    )


def _sha256_is_valid(value: str) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


__all__ = [
    "NAV2_TURTLEBOT3_EVIDENCE_KIND",
    "NAV2_TURTLEBOT3_OBSERVATION_REQUIREMENT_ID",
    "NAV2_TURTLEBOT3_OUTCOME_CLAIM_ID",
    "NAV2_TURTLEBOT3_OUTCOME_CLAIM_SCOPE",
    "NAV2_TURTLEBOT3_PREDICATE_PACKAGE_ID",
    "NAV2_TURTLEBOT3_PREDICATE_PACKAGE_SHA256",
    "NAV2_TURTLEBOT3_RESULT_SCHEMA_VERSION",
    "Nav2BridgeResponse",
    "Nav2PredicateStatus",
    "Nav2ProgressResult",
    "Nav2StateResult",
    "Nav2TurtleBot3BoundedDispatchResult",
    "Nav2TurtleBot3EvidenceBindings",
    "Nav2TurtleBot3PredicateContent",
    "Nav2TurtleBot3PredicateEvaluation",
    "Nav2TurtleBot3ReplayInput",
    "build_nav2_turtlebot3_replay_contract",
    "build_nav2_turtlebot3_replay_input",
    "evaluate_nav2_turtlebot3_predicate",
    "nav2_turtlebot3_predicate_package_binding",
]
