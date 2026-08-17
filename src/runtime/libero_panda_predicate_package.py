"""Concrete GR00T N1.7 / LIBERO Panda simulator predicate package.

This is the third concrete Mission Contract predicate package. It deliberately
does not define a shared predicate language and does not turn MissionOS into a
policy server, simulator controller, or safe-stop implementation.

The package evaluates one frozen, enumerated LIBERO simulator episode. It
recomputes the action-to-step lineage from typed action values before accepting
the pinned LIBERO task predicate. A synchronous ``env.step`` return remains
separate from an independently observed controller ACK.

Predicate satisfaction updates verifier state only. It never creates approval,
dispatch authority, an executor effect, operational closure, parent-mission
completion, or a next action.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from math import isfinite
from typing import Any, Literal

from missionos_core import (
    EvidenceOrigin,
    FrozenMissionContract,
    HardwareExecutionMode,
    MissionEvidenceReadiness,
    MissionObservation,
    MissionRuntimeEvidence,
    ObservationFreshnessBasis,
    ObservationRequirement,
    OutcomeClaimSpec,
    PredicatePackageBinding,
    QuantificationScope,
    QuantificationScopeKind,
    ReferenceInput,
    TerminationPolicy,
    TerminationReason,
    UTC_WALL_CLOCK_DOMAIN_REF,
    VerificationBasis,
    canonical_sha256,
    check_mission_evidence_readiness,
    mission_observation_source_receipt_binding_sha256,
)


LIBERO_PANDA_EPISODE_RESULT_SCHEMA_VERSION = "missionos_groot_n17_libero_panda_episode_result.v3"
LIBERO_PANDA_PREDICATE_PACKAGE_SCHEMA_VERSION = (
    "missionos_groot_n17_libero_panda_predicate_package.v2"
)
LIBERO_PANDA_PREDICATE_PACKAGE_ID = "groot_n17_libero_panda_official_sim_task_satisfied"
LIBERO_PANDA_PREDICATE_PACKAGE_VERSION = "2"
LIBERO_PANDA_OUTCOME_CLAIM_ID = "groot_n17_libero_panda_official_sim_task_satisfied"
LIBERO_PANDA_OUTCOME_CLAIM_SCOPE = "exact_groot_n17_libero_panda_simulator_episode"
LIBERO_PANDA_OBSERVATION_REQUIREMENT_ID = "libero_panda_episode_result"
LIBERO_PANDA_EVIDENCE_KIND = "groot_n17_libero_panda_episode_result"

ISAAC_GROOT_REVISION = "23ace64f17aa5015259b8609d371eb61a357c776"
LIBERO_REVISION = "8f1084e3132a39270c3a13ebe37270a43ece2a01"
GROOT_CHECKPOINT_REPOSITORY = "nvidia/GR00T-N1.7-LIBERO"
GROOT_CHECKPOINT_REVISION = "2ea293aa20ba7cf5bbf3ba17a5fbcb1a01cbfe21"
LIBERO_PANDA_EMBODIMENT_TAG = "LIBERO_PANDA"
LIBERO_PANDA_ENVIRONMENT = "libero_sim/KITCHEN_SCENE3_turn_on_the_stove_and_put_the_moka_pot_on_it"
LIBERO_TASK_BDDL_SHA256 = "491835cc2eb6956f7ce3d1ee4e266377116168fe453e6529fe105e3b0333635d"
LIBERO_ACTION_FIELDS = (
    "action.x",
    "action.y",
    "action.z",
    "action.roll",
    "action.pitch",
    "action.yaw",
    "action.gripper",
)
LIBERO_POLICY_ACTION_HORIZON = 16
GROOT_MODEL_ACTION_HORIZON_CAPACITY = 40
LIBERO_BASE_TRANSFORMATIONS = (
    "concatenate_libero_action_fields",
    "normalize_gripper_to_signed_range",
    "binarize_gripper_sign",
    "invert_gripper_sign",
)
LIBERO_FOUR_DIMENSIONAL_PROJECTION = "project_osc_position_to_xyz_gripper"

_TASK_PREDICATE_MATERIAL = {
    "isaac_groot_revision": ISAAC_GROOT_REVISION,
    "libero_revision": LIBERO_REVISION,
    "environment": LIBERO_PANDA_ENVIRONMENT,
    "bddl_sha256": LIBERO_TASK_BDDL_SHA256,
    "goal_predicates": [
        "flat_stove_1 is turned on",
        "moka_pot_1 is on flat_stove_1_cook_region",
    ],
    "combination": "logical_conjunction",
    "scope": (
        "the pinned LIBERO simulator environment and this exact episode; "
        "not a real-world stove or general semantic-completion claim"
    ),
}
LIBERO_TASK_PREDICATE_SHA256 = canonical_sha256(_TASK_PREDICATE_MATERIAL)

_PACKAGE_MATERIAL = {
    "schema_version": LIBERO_PANDA_PREDICATE_PACKAGE_SCHEMA_VERSION,
    "package_id": LIBERO_PANDA_PREDICATE_PACKAGE_ID,
    "package_version": LIBERO_PANDA_PREDICATE_PACKAGE_VERSION,
    "outcome_claim_id": LIBERO_PANDA_OUTCOME_CLAIM_ID,
    "outcome_claim_scope": LIBERO_PANDA_OUTCOME_CLAIM_SCOPE,
    "source_revisions": {
        "isaac_groot": ISAAC_GROOT_REVISION,
        "libero": LIBERO_REVISION,
        "checkpoint_repository": GROOT_CHECKPOINT_REPOSITORY,
        "checkpoint_revision": GROOT_CHECKPOINT_REVISION,
    },
    "environment": LIBERO_PANDA_ENVIRONMENT,
    "task_predicate_sha256": LIBERO_TASK_PREDICATE_SHA256,
    "required_checks": [
        "exact_run_and_episode_identity",
        "ordered_policy_action_chunks",
        "pinned_policy_and_execution_horizons",
        "ordered_chunk_step_slices",
        "declared_action_transformations_only",
        "exact_env_step_inputs",
        "simulator_step_returns_content_bound",
        "same_episode_official_predicate_true",
        "controller_ack_not_promoted",
        "simulator_scope_only",
    ],
    "requested_runtime_effect": "none",
}
LIBERO_PANDA_PREDICATE_PACKAGE_SHA256 = canonical_sha256(_PACKAGE_MATERIAL)

LIBERO_PANDA_SCENE8_ENVIRONMENT = "libero_sim/KITCHEN_SCENE8_put_both_moka_pots_on_the_stove"
LIBERO_PANDA_SCENE8_TASK_BDDL_SHA256 = (
    "ae1299a707d3e810096ad648948c517de40474bcdacca93a15e17d191e34454a"
)
LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_ID = (
    "groot_n17_libero_panda_scene8_official_sim_task_satisfied"
)
LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_VERSION = "2"
LIBERO_PANDA_SCENE8_OUTCOME_CLAIM_ID = "groot_n17_libero_panda_scene8_official_sim_task_satisfied"
_SCENE8_TASK_PREDICATE_MATERIAL = {
    "isaac_groot_revision": ISAAC_GROOT_REVISION,
    "libero_revision": LIBERO_REVISION,
    "environment": LIBERO_PANDA_SCENE8_ENVIRONMENT,
    "bddl_sha256": LIBERO_PANDA_SCENE8_TASK_BDDL_SHA256,
    "goal_predicates": [
        "moka_pot_1 is on flat_stove_1_cook_region",
        "moka_pot_2 is on flat_stove_1_cook_region",
        "flat_stove_1 is turned on",
    ],
    "combination": "logical_conjunction",
    "scope": (
        "the pinned LIBERO simulator environment and this exact episode; "
        "not a real-world stove or general semantic-completion claim"
    ),
}
LIBERO_PANDA_SCENE8_TASK_PREDICATE_SHA256 = canonical_sha256(_SCENE8_TASK_PREDICATE_MATERIAL)
_SCENE8_PACKAGE_MATERIAL = {
    **_PACKAGE_MATERIAL,
    "package_id": LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_ID,
    "package_version": LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_VERSION,
    "outcome_claim_id": LIBERO_PANDA_SCENE8_OUTCOME_CLAIM_ID,
    "environment": LIBERO_PANDA_SCENE8_ENVIRONMENT,
    "task_predicate_sha256": LIBERO_PANDA_SCENE8_TASK_PREDICATE_SHA256,
    "required_checks": [
        *_PACKAGE_MATERIAL["required_checks"],
        "same_step_individual_goal_predicates",
        "goal_predicate_conjunction_matches_official_result",
    ],
}
LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_SHA256 = canonical_sha256(_SCENE8_PACKAGE_MATERIAL)


def libero_panda_task_material(environment: str) -> dict[str, str]:
    """Return the frozen predicate binding for one explicitly supported task."""

    if environment == LIBERO_PANDA_ENVIRONMENT:
        return {
            "package_id": LIBERO_PANDA_PREDICATE_PACKAGE_ID,
            "package_version": LIBERO_PANDA_PREDICATE_PACKAGE_VERSION,
            "package_sha256": LIBERO_PANDA_PREDICATE_PACKAGE_SHA256,
            "outcome_claim_id": LIBERO_PANDA_OUTCOME_CLAIM_ID,
            "outcome_claim_scope": LIBERO_PANDA_OUTCOME_CLAIM_SCOPE,
            "task_predicate_sha256": LIBERO_TASK_PREDICATE_SHA256,
        }
    if environment == LIBERO_PANDA_SCENE8_ENVIRONMENT:
        return {
            "package_id": LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_ID,
            "package_version": LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_VERSION,
            "package_sha256": LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_SHA256,
            "outcome_claim_id": LIBERO_PANDA_SCENE8_OUTCOME_CLAIM_ID,
            "outcome_claim_scope": LIBERO_PANDA_OUTCOME_CLAIM_SCOPE,
            "task_predicate_sha256": (LIBERO_PANDA_SCENE8_TASK_PREDICATE_SHA256),
        }
    raise ValueError("libero_panda_environment_not_catalogued")


def libero_panda_goal_predicate_specs(
    environment: str,
) -> tuple[tuple[str, ...], ...]:
    """Return the exact ordered BDDL goal-state members for one task."""

    if environment == LIBERO_PANDA_ENVIRONMENT:
        return (
            ("turnon", "flat_stove_1"),
            ("on", "moka_pot_1", "flat_stove_1_cook_region"),
        )
    if environment == LIBERO_PANDA_SCENE8_ENVIRONMENT:
        return (
            ("on", "moka_pot_1", "flat_stove_1_cook_region"),
            ("on", "moka_pot_2", "flat_stove_1_cook_region"),
            ("turnon", "flat_stove_1"),
        )
    raise ValueError("libero_panda_environment_not_catalogued")


class LIBEROPandaPredicateStatus(str, Enum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class LIBEROPandaRunnerConfiguration:
    """Approval-frozen exact official-runner configuration."""

    model_repository: str
    checkpoint_revision: str
    isaac_groot_revision: str
    libero_revision: str
    embodiment_tag: str
    environment: str
    maximum_episode_steps: int
    policy_action_horizon: int
    n_action_steps: int
    n_envs: int
    controller_configuration_sha256: str
    action_dim: int
    terminate_on_success: bool
    policy_transport: str = "zmq_policy_client"
    runner: str = "official_rollout_policy"
    process_seed: int | None = None

    def to_material(self) -> dict[str, Any]:
        material = asdict(self)
        if self.process_seed is None:
            material.pop("process_seed")
        return material

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.to_material())


@dataclass(frozen=True)
class LIBEROPandaActionField:
    """One named action field across a complete policy chunk."""

    field_name: str
    values: tuple[float, ...]

    def to_material(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "values": list(self.values),
        }


@dataclass(frozen=True)
class LIBEROPandaActionChunk:
    """One policy response chunk retained before simulator dispatch."""

    chunk_index: int
    policy_request_sha256: str
    policy_response_sha256: str
    fields: tuple[LIBEROPandaActionField, ...]

    def to_material(self) -> dict[str, Any]:
        return {
            "chunk_index": self.chunk_index,
            "policy_request_sha256": self.policy_request_sha256,
            "policy_response_sha256": self.policy_response_sha256,
            "fields": [field.to_material() for field in self.fields],
        }

    @property
    def action_chunk_sha256(self) -> str:
        return canonical_sha256(self.to_material())


@dataclass(frozen=True)
class LIBEROPandaGoalPredicateObservation:
    """One simulator-ground-truth BDDL predicate observed after a step."""

    predicate_index: int
    predicate_name: str
    arguments: tuple[str, ...]
    satisfied: bool

    @property
    def predicate_id(self) -> str:
        return canonical_sha256(
            {
                "predicate_index": self.predicate_index,
                "predicate_name": self.predicate_name,
                "arguments": list(self.arguments),
            }
        )

    def to_material(self) -> dict[str, Any]:
        return {
            "predicate_id": self.predicate_id,
            "predicate_index": self.predicate_index,
            "predicate_name": self.predicate_name,
            "arguments": list(self.arguments),
            "satisfied": self.satisfied,
        }


@dataclass(frozen=True)
class LIBEROPandaStepApplication:
    """One ordered simulator-step input and its returned result digest."""

    global_step_index: int
    chunk_index: int
    chunk_step_index: int
    action_chunk_sha256: str
    transformation_names: tuple[str, ...]
    env_step_input: tuple[float, ...]
    simulator_step_return_sha256: str
    result_observation_sha256: str
    goal_predicate_observations: tuple[LIBEROPandaGoalPredicateObservation, ...]
    official_predicate_result: bool
    terminated: bool
    truncated: bool

    def to_material(self) -> dict[str, Any]:
        return {
            "global_step_index": self.global_step_index,
            "chunk_index": self.chunk_index,
            "chunk_step_index": self.chunk_step_index,
            "action_chunk_sha256": self.action_chunk_sha256,
            "transformation_names": list(self.transformation_names),
            "env_step_input": list(self.env_step_input),
            "simulator_step_return_sha256": (self.simulator_step_return_sha256),
            "result_observation_sha256": self.result_observation_sha256,
            "goal_predicate_observations": [
                observation.to_material() for observation in self.goal_predicate_observations
            ],
            "official_predicate_result": self.official_predicate_result,
            "terminated": self.terminated,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class LIBEROPandaPredicateContent:
    """Typed transient content addressed by one MissionObservation digest.

    Raw fixture/action values are available to the evaluator so lineage is
    recomputed rather than accepted as a caller-provided boolean. Publication
    artifacts must retain only derived manifests and digests.
    """

    source_schema_version: str
    run_identity: str
    episode_identity: str
    runner_configuration: LIBEROPandaRunnerConfiguration
    runtime_controller_configuration_sha256: str
    runtime_action_dim: int
    task_predicate_sha256: str
    action_chunks: tuple[LIBEROPandaActionChunk, ...]
    step_applications: tuple[LIBEROPandaStepApplication, ...]
    official_runner_episode_ended: bool
    official_runner_episode_success: bool
    observed_at: str
    received_at: str
    source_clock_domain_ref: str = UTC_WALL_CLOCK_DOMAIN_REF
    receipt_clock_domain_ref: str = UTC_WALL_CLOCK_DOMAIN_REF
    controller_ack_observed: bool = False
    readback_satisfies_controller_ack: bool = False
    physical_execution_invoked: bool = False

    def to_material(self) -> dict[str, Any]:
        return {
            "source_schema_version": self.source_schema_version,
            "run_identity": self.run_identity,
            "episode_identity": self.episode_identity,
            "runner_configuration": self.runner_configuration.to_material(),
            "runtime_controller_configuration_sha256": (
                self.runtime_controller_configuration_sha256
            ),
            "runtime_action_dim": self.runtime_action_dim,
            "task_predicate_sha256": self.task_predicate_sha256,
            "action_chunks": [chunk.to_material() for chunk in self.action_chunks],
            "step_applications": [step.to_material() for step in self.step_applications],
            "official_runner_episode_ended": (self.official_runner_episode_ended),
            "official_runner_episode_success": (self.official_runner_episode_success),
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "source_clock_domain_ref": self.source_clock_domain_ref,
            "receipt_clock_domain_ref": self.receipt_clock_domain_ref,
            "controller_ack_observed": self.controller_ack_observed,
            "readback_satisfies_controller_ack": (self.readback_satisfies_controller_ack),
            "physical_execution_invoked": self.physical_execution_invoked,
        }

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.to_material())

    @property
    def raw_action_stream_manifest_sha256(self) -> str:
        return canonical_sha256(
            {"action_chunks": [chunk.to_material() for chunk in self.action_chunks]}
        )

    @property
    def env_step_input_stream_manifest_sha256(self) -> str:
        return canonical_sha256(
            {
                "env_step_inputs": [
                    {
                        "global_step_index": step.global_step_index,
                        "chunk_index": step.chunk_index,
                        "chunk_step_index": step.chunk_step_index,
                        "action_chunk_sha256": (step.action_chunk_sha256),
                        "transformation_names": list(step.transformation_names),
                        "env_step_input": list(step.env_step_input),
                    }
                    for step in self.step_applications
                ]
            }
        )

    @property
    def simulator_step_return_manifest_sha256(self) -> str:
        return canonical_sha256(
            {
                "simulator_step_returns": [
                    {
                        "global_step_index": step.global_step_index,
                        "simulator_step_return_sha256": (step.simulator_step_return_sha256),
                        "result_observation_sha256": (step.result_observation_sha256),
                        "official_predicate_result": (step.official_predicate_result),
                        "goal_predicate_observations": [
                            observation.to_material()
                            for observation in step.goal_predicate_observations
                        ],
                        "terminated": step.terminated,
                        "truncated": step.truncated,
                    }
                    for step in self.step_applications
                ]
            }
        )


@dataclass(frozen=True)
class LIBEROPandaReplayInput:
    content: LIBEROPandaPredicateContent
    evidence: MissionRuntimeEvidence


@dataclass(frozen=True)
class LIBEROPandaPredicateEvaluation:
    contract_id: str
    contract_sha256: str
    predicate_package_id: str
    predicate_package_version: str
    predicate_package_sha256: str
    outcome_claim_id: str
    outcome_claim_scope: str
    evaluated_at: str
    evaluation_clock_domain_ref: str
    observation_content_sha256: str
    raw_action_stream_manifest_sha256: str
    env_step_input_stream_manifest_sha256: str
    simulator_step_return_manifest_sha256: str
    evidence_readiness: MissionEvidenceReadiness
    status: LIBEROPandaPredicateStatus
    evaluated_outcome_claim: bool
    actual_verification_basis: VerificationBasis
    evidence_origins: tuple[EvidenceOrigin, ...]
    evidence_refs: tuple[str, ...]
    reasons: tuple[str, ...]
    predicate_package_evaluated: bool
    simulator_step_return_observed: bool
    official_sim_task_success: bool
    goal_predicate_observations: tuple[LIBEROPandaGoalPredicateObservation, ...]
    controller_ack_observed: Literal[False] = False
    controller_ack_verification_status: Literal["unverified"] = "unverified"
    readback_satisfies_controller_ack: Literal[False] = False
    approval_created: Literal[False] = False
    dispatch_authority_created: Literal[False] = False
    runtime_effect_requested: Literal[False] = False
    operational_closure_created: Literal[False] = False
    physical_execution_invoked: Literal[False] = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_sha256": self.contract_sha256,
            "predicate_package_id": self.predicate_package_id,
            "predicate_package_version": self.predicate_package_version,
            "predicate_package_sha256": self.predicate_package_sha256,
            "outcome_claim_id": self.outcome_claim_id,
            "outcome_claim_scope": self.outcome_claim_scope,
            "evaluated_at": self.evaluated_at,
            "evaluation_clock_domain_ref": (self.evaluation_clock_domain_ref),
            "observation_content_sha256": (self.observation_content_sha256),
            "raw_action_stream_manifest_sha256": (self.raw_action_stream_manifest_sha256),
            "env_step_input_stream_manifest_sha256": (self.env_step_input_stream_manifest_sha256),
            "simulator_step_return_manifest_sha256": (self.simulator_step_return_manifest_sha256),
            "evidence_readiness": self.evidence_readiness.value,
            "status": self.status.value,
            "evaluated_outcome_claim": self.evaluated_outcome_claim,
            "actual_verification_basis": (self.actual_verification_basis.value),
            "evidence_origins": [origin.value for origin in self.evidence_origins],
            "evidence_refs": list(self.evidence_refs),
            "reasons": list(self.reasons),
            "predicate_package_evaluated": (self.predicate_package_evaluated),
            "simulator_step_return_observed": (self.simulator_step_return_observed),
            "official_sim_task_success": self.official_sim_task_success,
            "goal_predicate_observations": [
                observation.to_material() for observation in self.goal_predicate_observations
            ],
            "satisfied_predicate_ids": [
                observation.predicate_id
                for observation in self.goal_predicate_observations
                if observation.satisfied
            ],
            "unsatisfied_predicate_ids": [
                observation.predicate_id
                for observation in self.goal_predicate_observations
                if not observation.satisfied
            ],
            "controller_ack_observed": self.controller_ack_observed,
            "controller_ack_verification_status": (self.controller_ack_verification_status),
            "readback_satisfies_controller_ack": (self.readback_satisfies_controller_ack),
            "approval_created": self.approval_created,
            "dispatch_authority_created": (self.dispatch_authority_created),
            "runtime_effect_requested": self.runtime_effect_requested,
            "operational_closure_created": (self.operational_closure_created),
            "physical_execution_invoked": self.physical_execution_invoked,
        }


def libero_panda_predicate_package_binding(
    environment: str = LIBERO_PANDA_ENVIRONMENT,
) -> PredicatePackageBinding:
    task = libero_panda_task_material(environment)
    return PredicatePackageBinding(
        package_id=task["package_id"],
        package_version=task["package_version"],
        content_sha256=task["package_sha256"],
    )


def libero_panda_transformation_spec(
    runner_configuration: LIBEROPandaRunnerConfiguration,
) -> dict[str, Any]:
    names = list(LIBERO_BASE_TRANSFORMATIONS)
    if runner_configuration.action_dim == 4:
        names.append(LIBERO_FOUR_DIMENSIONAL_PROJECTION)
    return {
        "action_fields": list(LIBERO_ACTION_FIELDS),
        "action_dim": runner_configuration.action_dim,
        "ordered_transformations": names,
        "source_revisions": {
            "isaac_groot": ISAAC_GROOT_REVISION,
            "libero": LIBERO_REVISION,
        },
    }


def validate_libero_panda_runner_configuration(
    configuration: LIBEROPandaRunnerConfiguration,
) -> tuple[str, ...]:
    try:
        libero_panda_task_material(configuration.environment)
    except ValueError:
        return ("libero_runner_configuration_environment_mismatch",)
    expected = {
        "model_repository": GROOT_CHECKPOINT_REPOSITORY,
        "checkpoint_revision": GROOT_CHECKPOINT_REVISION,
        "isaac_groot_revision": ISAAC_GROOT_REVISION,
        "libero_revision": LIBERO_REVISION,
        "embodiment_tag": LIBERO_PANDA_EMBODIMENT_TAG,
        "maximum_episode_steps": 720,
        "policy_action_horizon": LIBERO_POLICY_ACTION_HORIZON,
        "n_action_steps": 8,
        "n_envs": 1,
        "terminate_on_success": True,
        "policy_transport": "zmq_policy_client",
        "runner": "official_rollout_policy",
    }
    reasons = [
        f"libero_runner_configuration_{field}_mismatch"
        for field, value in expected.items()
        if getattr(configuration, field, None) != value
    ]
    if not _sha256_is_valid(configuration.controller_configuration_sha256):
        reasons.append("libero_runner_controller_configuration_digest_invalid")
    if isinstance(configuration.action_dim, bool) or configuration.action_dim not in (4, 7):
        reasons.append("libero_runner_action_dim_unsupported")
    if configuration.process_seed is not None and (
        isinstance(configuration.process_seed, bool)
        or not isinstance(configuration.process_seed, int)
        or configuration.process_seed < 0
    ):
        reasons.append("libero_runner_process_seed_invalid")
    return tuple(reasons)


def build_libero_panda_replay_contract(
    *,
    contract_id: str,
    contract_version: str,
    runner_configuration: LIBEROPandaRunnerConfiguration,
    run_identity: str,
    episode_identity: str,
    maximum_observation_age_seconds: float,
) -> FrozenMissionContract:
    runner_reasons = validate_libero_panda_runner_configuration(runner_configuration)
    if runner_reasons:
        raise ValueError("runner configuration is invalid: " + ",".join(runner_reasons))
    if not str(run_identity or "").strip():
        raise ValueError("run identity is required before reset")
    if not str(episode_identity or "").strip():
        raise ValueError("episode identity is required before the first policy request")

    task = libero_panda_task_material(runner_configuration.environment)
    return FrozenMissionContract(
        contract_id=contract_id,
        contract_version=contract_version,
        execution_scope=HardwareExecutionMode.SIM,
        reference_inputs=(
            ReferenceInput(
                input_id="approved_runner_configuration",
                kind="approved_pinned_official_runner",
                content_sha256=runner_configuration.content_sha256,
            ),
            ReferenceInput(
                input_id="approved_task_predicate",
                kind="approved_pinned_libero_simulator_predicate",
                content_sha256=task["task_predicate_sha256"],
            ),
            ReferenceInput(
                input_id="approved_action_transformations",
                kind="approved_ordered_action_transformation_spec",
                content_sha256=canonical_sha256(
                    libero_panda_transformation_spec(runner_configuration)
                ),
            ),
            ReferenceInput(
                input_id="approved_run_identity",
                kind="approved_pre_reset_run_identity",
                content_sha256=canonical_sha256({"run_identity": run_identity}),
            ),
            ReferenceInput(
                input_id="approved_episode_identity",
                kind="approved_pre_request_episode_identity",
                content_sha256=canonical_sha256({"episode_identity": episode_identity}),
            ),
        ),
        observation_requirements=(
            ObservationRequirement(
                requirement_id=LIBERO_PANDA_OBSERVATION_REQUIREMENT_ID,
                evidence_kind=LIBERO_PANDA_EVIDENCE_KIND,
                required_origin=EvidenceOrigin.STORED_ARTIFACT,
                maximum_age_seconds=maximum_observation_age_seconds,
                freshness_basis=(ObservationFreshnessBasis.SOURCE_OBSERVED_AT),
                source_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                receipt_clock_domain_ref=UTC_WALL_CLOCK_DOMAIN_REF,
                require_source_receipt_binding=True,
            ),
        ),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.ENUMERATED_MEMBERS,
            member_ids=(episode_identity,),
        ),
        outcome_claim_spec=OutcomeClaimSpec(
            claim_id=task["outcome_claim_id"],
            statement=(
                "The exact pinned LIBERO simulator episode, with "
                "content-bound action-to-step lineage, satisfied the exact "
                "pinned LIBERO task predicate."
            ),
            claim_scope=task["outcome_claim_scope"],
        ),
        predicate_package=libero_panda_predicate_package_binding(runner_configuration.environment),
        termination_policy=TerminationPolicy(
            allowed_reasons=(
                TerminationReason.EXPIRY,
                TerminationReason.OPERATOR_INTERRUPTION,
                TerminationReason.SAFE_STOP,
                TerminationReason.TERMINAL_PREDICATE_SATISFIED,
            ),
            maximum_duration_seconds=None,
        ),
        required_verification_basis=VerificationBasis.DETERMINISTIC,
    )


def build_libero_panda_replay_input(
    *,
    contract: FrozenMissionContract,
    content: LIBEROPandaPredicateContent,
) -> LIBEROPandaReplayInput:
    binding = mission_observation_source_receipt_binding_sha256(
        observation_id=f"libero-panda-episode:{content.episode_identity}",
        content_sha256=content.content_sha256,
        observed_at=content.observed_at,
        source_clock_domain_ref=content.source_clock_domain_ref,
        received_at=content.received_at,
        receipt_clock_domain_ref=content.receipt_clock_domain_ref,
    )
    observation = MissionObservation(
        observation_id=f"libero-panda-episode:{content.episode_identity}",
        requirement_id=LIBERO_PANDA_OBSERVATION_REQUIREMENT_ID,
        evidence_kind=LIBERO_PANDA_EVIDENCE_KIND,
        origin=EvidenceOrigin.STORED_ARTIFACT,
        observed_at=content.observed_at,
        content_sha256=content.content_sha256,
        execution_scope=HardwareExecutionMode.SIM,
        source_clock_domain_ref=content.source_clock_domain_ref,
        received_at=content.received_at,
        receipt_clock_domain_ref=content.receipt_clock_domain_ref,
        source_receipt_binding_sha256=binding,
    )
    return LIBEROPandaReplayInput(
        content=content,
        evidence=MissionRuntimeEvidence(
            contract_sha256=contract.contract_sha256,
            observations=(observation,),
        ),
    )


def evaluate_libero_panda_predicate(
    *,
    contract: FrozenMissionContract,
    replay: LIBEROPandaReplayInput,
    evaluated_at: str,
    evaluation_clock_domain_ref: str = UTC_WALL_CLOCK_DOMAIN_REF,
) -> LIBEROPandaPredicateEvaluation:
    readiness = check_mission_evidence_readiness(
        contract=contract,
        evidence=replay.evidence,
        evaluated_at=evaluated_at,
        evaluation_clock_domain_ref=evaluation_clock_domain_ref,
    )
    observation = (
        replay.evidence.observations[0] if len(replay.evidence.observations) == 1 else None
    )
    boundary_reasons = _boundary_reasons(
        contract=contract,
        content=replay.content,
        observation=observation,
    )
    if readiness.evidence_readiness is not MissionEvidenceReadiness.READY or boundary_reasons:
        reasons = (
            tuple(
                reason
                for reason in readiness.reasons
                if reason != "outcome_claim_requires_approved_predicate_package"
            )
            + boundary_reasons
        )
        return _evaluation(
            contract=contract,
            replay=replay,
            evaluated_at=evaluated_at,
            evaluation_clock_domain_ref=evaluation_clock_domain_ref,
            readiness=readiness.evidence_readiness,
            status=(
                LIBEROPandaPredicateStatus.BLOCKED
                if readiness.evidence_readiness is MissionEvidenceReadiness.REFUSED
                or boundary_reasons
                else LIBEROPandaPredicateStatus.UNVERIFIED
            ),
            evaluated_outcome_claim=False,
            actual_verification_basis=VerificationBasis.UNVERIFIED,
            reasons=reasons,
            predicate_package_evaluated=False,
            simulator_step_return_observed=False,
            official_sim_task_success=False,
        )

    lineage_reasons = _lineage_reasons(replay.content)
    if lineage_reasons:
        return _evaluation(
            contract=contract,
            replay=replay,
            evaluated_at=evaluated_at,
            evaluation_clock_domain_ref=evaluation_clock_domain_ref,
            readiness=MissionEvidenceReadiness.READY,
            status=LIBEROPandaPredicateStatus.BLOCKED,
            evaluated_outcome_claim=False,
            actual_verification_basis=VerificationBasis.DETERMINISTIC,
            reasons=lineage_reasons,
            predicate_package_evaluated=False,
            simulator_step_return_observed=bool(replay.content.step_applications),
            official_sim_task_success=False,
        )

    task_satisfied = bool(
        replay.content.step_applications
        and replay.content.step_applications[-1].official_predicate_result is True
    )
    return _evaluation(
        contract=contract,
        replay=replay,
        evaluated_at=evaluated_at,
        evaluation_clock_domain_ref=evaluation_clock_domain_ref,
        readiness=MissionEvidenceReadiness.READY,
        status=(
            LIBEROPandaPredicateStatus.SATISFIED
            if task_satisfied
            else LIBEROPandaPredicateStatus.NOT_SATISFIED
        ),
        evaluated_outcome_claim=task_satisfied,
        actual_verification_basis=VerificationBasis.DETERMINISTIC,
        reasons=(() if task_satisfied else ("official_sim_task_predicate_not_satisfied",)),
        predicate_package_evaluated=True,
        simulator_step_return_observed=True,
        official_sim_task_success=task_satisfied,
    )


def _boundary_reasons(
    *,
    contract: FrozenMissionContract,
    content: LIBEROPandaPredicateContent,
    observation: MissionObservation | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    try:
        task = libero_panda_task_material(content.runner_configuration.environment)
    except ValueError:
        return ("predicate_package_binding_mismatch",)
    if (
        contract.predicate_package
        != libero_panda_predicate_package_binding(content.runner_configuration.environment)
        or contract.outcome_claim_spec.claim_id != task["outcome_claim_id"]
        or contract.outcome_claim_spec.claim_scope != task["outcome_claim_scope"]
    ):
        reasons.append("predicate_package_binding_mismatch")

    references = {item.input_id: item.content_sha256 for item in contract.reference_inputs}
    expected_references = {
        "approved_runner_configuration": (content.runner_configuration.content_sha256),
        "approved_task_predicate": content.task_predicate_sha256,
        "approved_action_transformations": canonical_sha256(
            libero_panda_transformation_spec(content.runner_configuration)
        ),
        "approved_run_identity": canonical_sha256({"run_identity": content.run_identity}),
        "approved_episode_identity": canonical_sha256(
            {"episode_identity": content.episode_identity}
        ),
    }
    for reference_id, expected_sha256 in expected_references.items():
        if references.get(reference_id) != expected_sha256:
            reasons.append(f"{reference_id}_mismatch")

    if (
        contract.quantification_scope.kind is not QuantificationScopeKind.ENUMERATED_MEMBERS
        or contract.quantification_scope.member_ids != (content.episode_identity,)
    ):
        reasons.append("episode_quantification_scope_mismatch")

    if (
        observation is None
        or observation.content_sha256 != content.content_sha256
        or observation.observed_at != content.observed_at
        or observation.received_at != content.received_at
        or observation.source_clock_domain_ref != content.source_clock_domain_ref
        or observation.receipt_clock_domain_ref != content.receipt_clock_domain_ref
    ):
        reasons.append("predicate_observation_content_binding_mismatch")
    return tuple(reasons)


def _lineage_reasons(
    content: LIBEROPandaPredicateContent,
) -> tuple[str, ...]:
    reasons: list[str] = []
    try:
        task = libero_panda_task_material(content.runner_configuration.environment)
    except ValueError:
        task = None
        reasons.append("task_environment_not_catalogued")
    if content.source_schema_version != LIBERO_PANDA_EPISODE_RESULT_SCHEMA_VERSION:
        reasons.append("source_schema_invalid")
    reasons.extend(validate_libero_panda_runner_configuration(content.runner_configuration))
    if task is None or content.task_predicate_sha256 != task["task_predicate_sha256"]:
        reasons.append("task_predicate_digest_invalid")
    if (
        content.runtime_controller_configuration_sha256
        != content.runner_configuration.controller_configuration_sha256
    ):
        reasons.append("runtime_controller_configuration_digest_mismatch")
    if (
        isinstance(content.runtime_action_dim, bool)
        or content.runtime_action_dim != content.runner_configuration.action_dim
    ):
        reasons.append("runtime_action_dim_mismatch")
    if not str(content.run_identity or "").strip():
        reasons.append("run_identity_missing")
    if not str(content.episode_identity or "").strip():
        reasons.append("episode_identity_missing")
    if content.source_clock_domain_ref != UTC_WALL_CLOCK_DOMAIN_REF:
        reasons.append("source_clock_domain_invalid")
    if content.receipt_clock_domain_ref != UTC_WALL_CLOCK_DOMAIN_REF:
        reasons.append("receipt_clock_domain_invalid")
    if content.controller_ack_observed is not False:
        reasons.append("independent_controller_ack_claim_forbidden")
    if content.readback_satisfies_controller_ack is not False:
        reasons.append("readback_ack_substitution_forbidden")
    if content.physical_execution_invoked is not False:
        reasons.append("physical_execution_claim_forbidden")

    chunks = content.action_chunks
    if not chunks:
        reasons.append("policy_action_chunk_missing")
    chunk_by_index: dict[int, LIBEROPandaActionChunk] = {}
    for expected_index, chunk in enumerate(chunks):
        if isinstance(chunk.chunk_index, bool) or chunk.chunk_index != expected_index:
            reasons.append("policy_action_chunk_order_invalid")
        chunk_by_index[chunk.chunk_index] = chunk
        if not _sha256_is_valid(chunk.policy_request_sha256):
            reasons.append(f"policy_request_digest_invalid:{chunk.chunk_index}")
        if not _sha256_is_valid(chunk.policy_response_sha256):
            reasons.append(f"policy_response_digest_invalid:{chunk.chunk_index}")
        if tuple(field.field_name for field in chunk.fields) != (LIBERO_ACTION_FIELDS):
            reasons.append(f"policy_action_fields_invalid:{chunk.chunk_index}")
            continue
        for field in chunk.fields:
            if len(field.values) != content.runner_configuration.policy_action_horizon:
                reasons.append(
                    f"policy_action_horizon_invalid:{chunk.chunk_index}:{field.field_name}"
                )
            if any(not _finite_number(value) for value in field.values):
                reasons.append(f"policy_action_non_finite:{chunk.chunk_index}:{field.field_name}")

    steps = content.step_applications
    if not steps:
        reasons.append("simulator_step_application_missing")
    expected_step_by_chunk: dict[int, int] = {}
    terminal_seen = False
    chunks_with_steps: set[int] = set()
    last_chunk_index = -1
    for expected_global_index, step in enumerate(steps):
        if (
            isinstance(step.global_step_index, bool)
            or step.global_step_index != expected_global_index
        ):
            reasons.append("simulator_global_step_order_invalid")
        if terminal_seen:
            reasons.append("simulator_step_after_terminal_result")
        if (
            not isinstance(step.official_predicate_result, bool)
            or not isinstance(step.terminated, bool)
            or not isinstance(step.truncated, bool)
        ):
            reasons.append(f"simulator_step_flags_invalid:{expected_global_index}")
        terminal_seen = (
            step.official_predicate_result is True
            or step.terminated is True
            or step.truncated is True
        )

        chunk = chunk_by_index.get(step.chunk_index)
        if chunk is None:
            reasons.append(f"simulator_step_chunk_missing:{expected_global_index}")
            continue
        if (
            not isinstance(step.chunk_index, int)
            or isinstance(step.chunk_index, bool)
            or step.chunk_index < last_chunk_index
            or step.chunk_index > last_chunk_index + 1
        ):
            reasons.append("simulator_chunk_order_invalid")
        else:
            last_chunk_index = step.chunk_index
        chunks_with_steps.add(step.chunk_index)
        expected_chunk_step = expected_step_by_chunk.get(step.chunk_index, 0)
        if isinstance(step.chunk_step_index, bool) or step.chunk_step_index != expected_chunk_step:
            reasons.append(f"simulator_chunk_step_order_invalid:{step.chunk_index}")
        expected_step_by_chunk[step.chunk_index] = expected_chunk_step + 1
        if (
            step.chunk_step_index < 0
            or step.chunk_step_index >= content.runner_configuration.n_action_steps
        ):
            reasons.append(f"simulator_chunk_step_index_invalid:{expected_global_index}")
            continue
        if step.action_chunk_sha256 != chunk.action_chunk_sha256:
            reasons.append(f"simulator_step_chunk_digest_mismatch:{expected_global_index}")

        expected_transformations = tuple(
            libero_panda_transformation_spec(content.runner_configuration)[
                "ordered_transformations"
            ]
        )
        if step.transformation_names != expected_transformations:
            reasons.append(f"undeclared_action_transformation:{expected_global_index}")
        expected_input = _expected_env_step_input(
            chunk=chunk,
            chunk_step_index=step.chunk_step_index,
            action_dim=content.runner_configuration.action_dim,
        )
        if expected_input is None:
            reasons.append(f"simulator_step_input_uncomputable:{expected_global_index}")
        elif canonical_sha256({"env_step_input": list(step.env_step_input)}) != canonical_sha256(
            {"env_step_input": list(expected_input)}
        ):
            reasons.append(f"simulator_step_input_mismatch:{expected_global_index}")
        if any(not _finite_number(value) for value in step.env_step_input):
            reasons.append(f"simulator_step_input_non_finite:{expected_global_index}")
        if not _sha256_is_valid(step.simulator_step_return_sha256):
            reasons.append(f"simulator_step_return_digest_invalid:{expected_global_index}")
        if not _sha256_is_valid(step.result_observation_sha256):
            reasons.append(f"simulator_result_observation_digest_invalid:{expected_global_index}")
        try:
            expected_goal_specs = libero_panda_goal_predicate_specs(
                content.runner_configuration.environment
            )
        except ValueError:
            expected_goal_specs = ()
        observed_goal_specs = tuple(
            (observation.predicate_name, *observation.arguments)
            for observation in step.goal_predicate_observations
        )
        if observed_goal_specs != expected_goal_specs:
            reasons.append(f"goal_predicate_vector_definition_mismatch:{expected_global_index}")
        if any(
            isinstance(observation.predicate_index, bool)
            or observation.predicate_index != predicate_index
            or not isinstance(observation.satisfied, bool)
            for predicate_index, observation in enumerate(step.goal_predicate_observations)
        ):
            reasons.append(f"goal_predicate_vector_value_invalid:{expected_global_index}")
        if (
            bool(step.goal_predicate_observations)
            and all(observation.satisfied for observation in step.goal_predicate_observations)
            is not step.official_predicate_result
        ):
            reasons.append(f"goal_predicate_conjunction_mismatch:{expected_global_index}")

    if chunks and chunks_with_steps != set(chunk_by_index):
        reasons.append("policy_action_chunk_without_simulator_step")
    for chunk in chunks[:-1]:
        if (
            expected_step_by_chunk.get(chunk.chunk_index)
            != content.runner_configuration.n_action_steps
        ):
            reasons.append(f"nonfinal_policy_chunk_incomplete:{chunk.chunk_index}")
    if steps and any(step.official_predicate_result is True for step in steps[:-1]):
        reasons.append("official_predicate_true_before_final_step")
    if steps and not (
        steps[-1].official_predicate_result is True
        or steps[-1].terminated is True
        or steps[-1].truncated is True
        or content.official_runner_episode_ended is True
    ):
        reasons.append("episode_terminal_result_missing")
    if content.official_runner_episode_ended is not True:
        reasons.append("official_runner_episode_end_missing")
    if (
        not isinstance(content.official_runner_episode_success, bool)
        or not steps
        or content.official_runner_episode_success is not steps[-1].official_predicate_result
    ):
        reasons.append("official_runner_episode_success_mismatch")
    return tuple(dict.fromkeys(reasons))


def _expected_env_step_input(
    *,
    chunk: LIBEROPandaActionChunk,
    chunk_step_index: int,
    action_dim: int,
) -> tuple[float, ...] | None:
    fields = {field.field_name: field.values for field in chunk.fields}
    try:
        vector = [
            float(fields[field_name][chunk_step_index]) for field_name in LIBERO_ACTION_FIELDS
        ]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    gripper = 2.0 * vector[-1] - 1.0
    gripper = 1.0 if gripper > 0 else -1.0 if gripper < 0 else 0.0
    vector[-1] = gripper * -1.0
    if action_dim == 4:
        vector = [*vector[:3], vector[-1]]
    return tuple(vector)


def _evaluation(
    *,
    contract: FrozenMissionContract,
    replay: LIBEROPandaReplayInput,
    evaluated_at: str,
    evaluation_clock_domain_ref: str,
    readiness: MissionEvidenceReadiness,
    status: LIBEROPandaPredicateStatus,
    evaluated_outcome_claim: bool,
    actual_verification_basis: VerificationBasis,
    reasons: tuple[str, ...],
    predicate_package_evaluated: bool,
    simulator_step_return_observed: bool,
    official_sim_task_success: bool,
) -> LIBEROPandaPredicateEvaluation:
    task = libero_panda_task_material(replay.content.runner_configuration.environment)
    return LIBEROPandaPredicateEvaluation(
        contract_id=contract.contract_id,
        contract_sha256=contract.contract_sha256,
        predicate_package_id=task["package_id"],
        predicate_package_version=task["package_version"],
        predicate_package_sha256=task["package_sha256"],
        outcome_claim_id=task["outcome_claim_id"],
        outcome_claim_scope=task["outcome_claim_scope"],
        evaluated_at=evaluated_at,
        evaluation_clock_domain_ref=evaluation_clock_domain_ref,
        observation_content_sha256=replay.content.content_sha256,
        raw_action_stream_manifest_sha256=(replay.content.raw_action_stream_manifest_sha256),
        env_step_input_stream_manifest_sha256=(
            replay.content.env_step_input_stream_manifest_sha256
        ),
        simulator_step_return_manifest_sha256=(
            replay.content.simulator_step_return_manifest_sha256
        ),
        evidence_readiness=readiness,
        status=status,
        evaluated_outcome_claim=evaluated_outcome_claim,
        actual_verification_basis=actual_verification_basis,
        evidence_origins=tuple(observation.origin for observation in replay.evidence.observations),
        evidence_refs=tuple(
            f"mission-observation:{observation.observation_id}"
            for observation in replay.evidence.observations
        ),
        reasons=tuple(dict.fromkeys(reasons)),
        predicate_package_evaluated=predicate_package_evaluated,
        simulator_step_return_observed=simulator_step_return_observed,
        official_sim_task_success=official_sim_task_success,
        goal_predicate_observations=(
            replay.content.step_applications[-1].goal_predicate_observations
            if replay.content.step_applications
            else ()
        ),
    )


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(float(value))
    )


def _sha256_is_valid(value: str) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


__all__ = [
    "GROOT_CHECKPOINT_REPOSITORY",
    "GROOT_CHECKPOINT_REVISION",
    "GROOT_MODEL_ACTION_HORIZON_CAPACITY",
    "ISAAC_GROOT_REVISION",
    "LIBERO_ACTION_FIELDS",
    "LIBERO_BASE_TRANSFORMATIONS",
    "LIBERO_FOUR_DIMENSIONAL_PROJECTION",
    "LIBERO_POLICY_ACTION_HORIZON",
    "LIBERO_PANDA_EMBODIMENT_TAG",
    "LIBERO_PANDA_ENVIRONMENT",
    "LIBERO_PANDA_EPISODE_RESULT_SCHEMA_VERSION",
    "LIBERO_PANDA_EVIDENCE_KIND",
    "LIBERO_PANDA_OBSERVATION_REQUIREMENT_ID",
    "LIBERO_PANDA_OUTCOME_CLAIM_ID",
    "LIBERO_PANDA_OUTCOME_CLAIM_SCOPE",
    "LIBERO_PANDA_PREDICATE_PACKAGE_ID",
    "LIBERO_PANDA_PREDICATE_PACKAGE_SHA256",
    "LIBERO_PANDA_PREDICATE_PACKAGE_VERSION",
    "LIBERO_PANDA_SCENE8_ENVIRONMENT",
    "LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_ID",
    "LIBERO_PANDA_SCENE8_PREDICATE_PACKAGE_SHA256",
    "LIBERO_PANDA_SCENE8_TASK_BDDL_SHA256",
    "LIBERO_PANDA_SCENE8_TASK_PREDICATE_SHA256",
    "LIBERO_REVISION",
    "LIBERO_TASK_BDDL_SHA256",
    "LIBERO_TASK_PREDICATE_SHA256",
    "LIBEROPandaActionChunk",
    "LIBEROPandaActionField",
    "LIBEROPandaGoalPredicateObservation",
    "LIBEROPandaPredicateContent",
    "LIBEROPandaPredicateEvaluation",
    "LIBEROPandaPredicateStatus",
    "LIBEROPandaReplayInput",
    "LIBEROPandaRunnerConfiguration",
    "LIBEROPandaStepApplication",
    "build_libero_panda_replay_contract",
    "build_libero_panda_replay_input",
    "evaluate_libero_panda_predicate",
    "libero_panda_predicate_package_binding",
    "libero_panda_goal_predicate_specs",
    "libero_panda_task_material",
    "libero_panda_transformation_spec",
    "validate_libero_panda_runner_configuration",
]
