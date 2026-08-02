"""Approved concrete Physical AI mission catalog for the chat surface.

Natural-language input may select one of these packages.  It cannot invent
child predicates, executor parameters, stage order, or runtime authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping
from uuid import uuid4

from missionos_core import (
    FrozenMissionContract,
    FrozenParentMissionContract,
    ParentMissionApprovalBinding,
    QuantificationScope,
    QuantificationScopeKind,
    build_parent_mission_approval_binding,
    build_parent_mission_stage_binding,
    canonical_sha256,
)

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.libero_panda_predicate_package import (
    GROOT_CHECKPOINT_REPOSITORY,
    GROOT_CHECKPOINT_REVISION,
    ISAAC_GROOT_REVISION,
    LIBERO_PANDA_EMBODIMENT_TAG,
    LIBERO_PANDA_ENVIRONMENT,
    LIBERO_POLICY_ACTION_HORIZON,
    LIBERO_REVISION,
    LIBERO_TASK_PREDICATE_SHA256,
    LIBEROPandaRunnerConfiguration,
    build_libero_panda_replay_contract,
)
from src.runtime.nav2_turtlebot3_predicate_package import (
    build_nav2_turtlebot3_replay_contract,
)
from src.runtime.px4_gazebo_delivery_predicate_package import (
    build_px4_gazebo_delivery_replay_contract,
)


PHYSICAL_AI_PROPOSAL_SCHEMA_VERSION = "missionos_physical_ai_chat_proposal.v2"
PHYSICAL_AI_APPROVAL_SCHEMA_VERSION = "missionos_physical_ai_chat_approval.v2"
PHYSICAL_AI_VALIDATION_SCHEMA_VERSION = "missionos_physical_ai_chat_validation.v2"
VLA_TASK_CATALOG_SCHEMA_VERSION = "missionos_vla_task_catalog.v1"
VLA_TASK_RESOLVER_VERSION = "missionos_vla_task_resolver.v1"

VLA_ONLY_MISSION_KIND = "groot_libero_panda"
THREE_STAGE_MISSION_KIND = "px4_nav2_groot_libero"
PhysicalAIMissionKind = Literal[
    "groot_libero_panda",
    "px4_nav2_groot_libero",
]

PX4_STAGE_REF = "px4_gazebo_delivery"
NAV2_STAGE_REF = "nav2_turtlebot3_bounded_goal"
LIBERO_STAGE_REF = "groot_libero_panda"

PX4_EXECUTOR_REF = "sim:px4-gazebo-sitl-delivery"
NAV2_EXECUTOR_REF = "sim:nav2-turtlebot3-bounded-goal"
LIBERO_EXECUTOR_REF = "vla:groot-n17-libero-panda"

THREE_STAGE_AUTHORITY_BUNDLE_REF = "catalog:physical-ai-chat:three-stage:v1"
VLA_ONLY_AUTHORITY_BUNDLE_REF = "catalog:physical-ai-chat:libero-panda:v1"

LIBERO_STOVE_MOKA_TASK_CATALOG_ID = "libero-panda-stove-moka.v1"
LIBERO_STOVE_MOKA_INSTRUCTION = "turn on the stove and put the moka pot on it"
_LIBERO_STOVE_MOKA_APPROVED_ALIASES = frozenset(
    {
        "gr00tでコンロを点けてモカポットを置いて",
        "gr00tでストーブを点けてモカポットを置いて",
        "use gr00t to turn on the stove and put the moka pot on it",
        "gr00t: turn on the stove and put the moka pot on it",
    }
)
_GENERIC_VLA_CATALOG_REQUESTS = frozenset(
    {
        "gr00tでvlaミッションを実行して",
        "gr00tのvlaミッションを実行して",
        "vlaミッションを実行して",
        "run the approved physical ai mission",
        "run the approved gr00t mission",
        "approved vla mission",
        "px4、nav2、gr00tを統合管制して",
        "px4、nav2、gr00tを一つのミッションとして統合管制して",
    }
)


@dataclass(frozen=True)
class PhysicalAIRequestResolution:
    """Fail-closed result of resolving operator language to the task catalog."""

    requested: bool
    mission_kind: PhysicalAIMissionKind | None = None
    task_catalog_id: str | None = None
    match_kind: str | None = None
    rejection_reason: str | None = None

SHARED_TARGET_DESCRIPTOR = {
    "schema_version": "missionos_parent_shared_target_descriptor.v1",
    "descriptor_id": "physical-ai-chat:separate-simulator-worlds:v1",
    "mission_intent": (
        "Run approved simulator stages under one parent authority and evidence "
        "lineage."
    ),
    "simulation_world_count": 3,
    "physical_identity_asserted": False,
    "shared_world_asserted": False,
    "relationship": "ordered_governance_demonstration_only",
}

EXPECTED_LIBERO_CONTROLLER_MATERIAL = {
    "controller_name": "OSC_POSE",
    "controller_class": "OperationalSpaceController",
    "action_dim": 7,
    "arm_control_dim": 6,
    "gripper_dof": 1,
    "control_freq_hz": 20,
    "use_delta": True,
    "use_orientation": True,
    "impedance_mode": "fixed",
}


def _vla_task_catalog_entry_material() -> dict[str, Any]:
    base = {
        "schema_version": VLA_TASK_CATALOG_SCHEMA_VERSION,
        "catalog_entry_id": LIBERO_STOVE_MOKA_TASK_CATALOG_ID,
        "catalog_entry_version": "1",
        "display_name": "LIBERO Panda: turn on stove and place moka pot",
        "display_name_ja": "LIBERO Panda: コンロを点けてモカポットを置く",
        "approved_operator_aliases": sorted(
            _LIBERO_STOVE_MOKA_APPROVED_ALIASES
        ),
        "resolved_instruction": LIBERO_STOVE_MOKA_INSTRUCTION,
        "instruction_sha256": canonical_sha256(
            {"instruction": LIBERO_STOVE_MOKA_INSTRUCTION}
        ),
        "instruction_delivery_mode": "fixed_environment_task",
        "policy_instruction_delivery_claimed": False,
        "environment": LIBERO_PANDA_ENVIRONMENT,
        "task_predicate_sha256": LIBERO_TASK_PREDICATE_SHA256,
        "model_repository": GROOT_CHECKPOINT_REPOSITORY,
        "checkpoint_revision": GROOT_CHECKPOINT_REVISION,
        "embodiment_tag": LIBERO_PANDA_EMBODIMENT_TAG,
        "claim_scope": "one exact pinned LIBERO simulator episode",
    }
    return {**base, "content_sha256": canonical_sha256(base)}


def approved_vla_task_catalog() -> tuple[dict[str, Any], ...]:
    """Return publication-safe copies of the approved VLA task entries."""

    return (_vla_task_catalog_entry_material(),)


def _vla_task_resolver_sha256() -> str:
    return canonical_sha256(
        {
            "resolver_version": VLA_TASK_RESOLVER_VERSION,
            "task_aliases": sorted(_LIBERO_STOVE_MOKA_APPROVED_ALIASES),
            "generic_catalog_requests": sorted(_GENERIC_VLA_CATALOG_REQUESTS),
        }
    )


def _normalized_instruction(text: str) -> str:
    normalized = " ".join(
        str(text or "").casefold().replace("　", " ").split()
    )
    return normalized.rstrip("。.!?？")


def _contains_vla_request(text: str) -> bool:
    return any(
        token in text
        for token in (
            "vla",
            "gr00t",
            "groot",
            "libero",
            "panda",
            "vlaミッション",
        )
    )


def _matches_stove_moka_task(text: str) -> bool:
    return text in _LIBERO_STOVE_MOKA_APPROVED_ALIASES


def _is_generic_vla_catalog_request(text: str) -> bool:
    return text in _GENERIC_VLA_CATALOG_REQUESTS


def resolve_physical_ai_request(text: str) -> PhysicalAIRequestResolution:
    """Resolve language to one approved entry without inventing task semantics."""

    normalized = _normalized_instruction(text)
    if not _contains_vla_request(normalized):
        return PhysicalAIRequestResolution(requested=False)

    if _matches_stove_moka_task(normalized):
        match_kind = "approved_task_alias"
    elif _is_generic_vla_catalog_request(normalized):
        match_kind = "generic_catalog_request"
    else:
        return PhysicalAIRequestResolution(
            requested=True,
            rejection_reason="approved_vla_task_not_found",
        )

    px4_requested = any(
        token in normalized for token in ("px4", "ドローン", "gazebo")
    )
    nav2_requested = any(
        token in normalized for token in ("nav2", "turtlebot", "移動ロボット")
    )
    mission_kind: PhysicalAIMissionKind = (
        THREE_STAGE_MISSION_KIND
        if px4_requested and nav2_requested
        else VLA_ONLY_MISSION_KIND
    )
    return PhysicalAIRequestResolution(
        requested=True,
        mission_kind=mission_kind,
        task_catalog_id=LIBERO_STOVE_MOKA_TASK_CATALOG_ID,
        match_kind=match_kind,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def physical_ai_request_kind(text: str) -> PhysicalAIMissionKind | None:
    """Return a catalog selection only for an explicit VLA/GR00T request."""

    return resolve_physical_ai_request(text).mission_kind

def build_libero_runner_configuration() -> LIBEROPandaRunnerConfiguration:
    return LIBEROPandaRunnerConfiguration(
        model_repository=GROOT_CHECKPOINT_REPOSITORY,
        checkpoint_revision=GROOT_CHECKPOINT_REVISION,
        isaac_groot_revision=ISAAC_GROOT_REVISION,
        libero_revision=LIBERO_REVISION,
        embodiment_tag=LIBERO_PANDA_EMBODIMENT_TAG,
        environment=LIBERO_PANDA_ENVIRONMENT,
        maximum_episode_steps=720,
        policy_action_horizon=LIBERO_POLICY_ACTION_HORIZON,
        n_action_steps=8,
        n_envs=1,
        controller_configuration_sha256=canonical_sha256(
            EXPECTED_LIBERO_CONTROLLER_MATERIAL
        ),
        action_dim=7,
        terminate_on_success=True,
    )


def build_px4_contract() -> FrozenMissionContract:
    delivery = build_delivery_mission_contract(
        mission_id="sitl-e2e-delivery-epic-exit",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "latitude": 35.689487,
            "longitude": 139.691706,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-sitl-dropoff", "max_weight_kg": 5.0},
        weather_constraints={
            "max_wind_speed_mps": 6.0,
            "max_precipitation_mm_per_hour": 0.0,
            "min_visibility_m": 1500.0,
        },
        battery_policy={
            "minimum_takeoff_percent": 80,
            "return_to_home_percent": 35,
            "reserve_landing_percent": 25,
        },
        landing_zone_policy={
            "min_clear_radius_m": 3.0,
            "max_slope_degrees": 5.0,
            "accepted_surface_kinds": ["marked_pad"],
        },
        telemetry_requirements={
            "required_measurements": [
                "position",
                "battery_percent",
                "vehicle_health",
                "weather_snapshot",
            ],
            "max_freshness_seconds": 2.0,
        },
    )
    return build_px4_gazebo_delivery_replay_contract(
        contract_id="px4-gazebo-sitl-e2e-delivery",
        contract_version="2026-07-30",
        approved_drop_zone=delivery.dropoff_location.model_dump(mode="json"),
        approved_payload_release_rule={
            "event_source": "gazebo_detachable_joint_detach_event",
            "dropoff_zone_radius_m": 1.0,
            "altitude_tolerance_m": 0.5,
            "release_time_window_seconds": 5.0,
            "expected_mission_item_seq": 2,
        },
        approved_same_session_rule={
            "mission_upload_and_release_same_session": True,
            "mission_request_sequences": [0, 1, 2, 3],
        },
        maximum_observation_age_seconds=30.0,
    )


def build_nav2_contract() -> FrozenMissionContract:
    approved_goal_pose = {
        "frame_id": "map",
        "x_m": 0.75,
        "y_m": 0.0,
        "yaw_rad": 0.0,
        "tolerance_m": 0.25,
        "max_speed_mps": 0.25,
        "max_distance_m": 3.0,
        "label": "turtlebot3_short_nav2_goal",
    }
    return build_nav2_turtlebot3_replay_contract(
        contract_id="nav2-turtlebot3-bounded-goal",
        contract_version="2026-07-29",
        approved_goal_pose=approved_goal_pose,
        approved_goal_frame={"frame_id": "map"},
        maximum_observation_age_seconds=30.0,
    )


def build_libero_contract(
    *,
    parent_run_identity: str,
    episode_identity: str | None = None,
) -> FrozenMissionContract:
    resolved_episode = episode_identity or f"{parent_run_identity}:libero-episode-1"
    return build_libero_panda_replay_contract(
        contract_id=f"libero-panda-contract:{resolved_episode}",
        contract_version="v1",
        runner_configuration=build_libero_runner_configuration(),
        run_identity=parent_run_identity,
        episode_identity=resolved_episode,
        maximum_observation_age_seconds=30.0,
    )


def build_three_stage_bundle(
    *,
    parent_run_identity: str,
    operator_approval_ref: str | None = None,
) -> tuple[
    FrozenParentMissionContract,
    ParentMissionApprovalBinding | None,
    dict[str, FrozenMissionContract],
    str,
]:
    px4 = build_px4_contract()
    nav2 = build_nav2_contract()
    episode_identity = f"{parent_run_identity}:libero-episode-1"
    libero = build_libero_contract(
        parent_run_identity=parent_run_identity,
        episode_identity=episode_identity,
    )
    children = {
        PX4_STAGE_REF: px4,
        NAV2_STAGE_REF: nav2,
        LIBERO_STAGE_REF: libero,
    }
    parent = FrozenParentMissionContract(
        parent_mission_id=parent_run_identity,
        parent_mission_version="2026-08-01",
        shared_target_descriptor_sha256=canonical_sha256(SHARED_TARGET_DESCRIPTOR),
        quantification_scope=QuantificationScope(
            kind=QuantificationScopeKind.NONE,
            reason=(
                "The stages run in three separate simulator worlds. The parent "
                "proves ordered authority and evidence lineage only."
            ),
        ),
        stages=tuple(
            build_parent_mission_stage_binding(
                stage_index=index,
                stage_ref=stage_ref,
                executor_ref=executor_ref,
                child_contract=children[stage_ref],
            )
            for index, (stage_ref, executor_ref) in enumerate(
                (
                    (PX4_STAGE_REF, PX4_EXECUTOR_REF),
                    (NAV2_STAGE_REF, NAV2_EXECUTOR_REF),
                    (LIBERO_STAGE_REF, LIBERO_EXECUTOR_REF),
                ),
                start=1,
            )
        ),
    )
    approval = (
        build_parent_mission_approval_binding(
            contract=parent,
            operator_approval_ref=operator_approval_ref,
            authority_bundle_ref=THREE_STAGE_AUTHORITY_BUNDLE_REF,
        )
        if operator_approval_ref
        else None
    )
    return parent, approval, children, episode_identity


def _proposal_material(proposal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(proposal).items()
        if key != "proposal_sha256"
    }


def proposal_sha256(proposal: Mapping[str, Any]) -> str:
    return canonical_sha256(_proposal_material(proposal))


def build_physical_ai_proposal(
    *,
    operator_instruction: str,
    mission_kind: PhysicalAIMissionKind,
    now: datetime | None = None,
) -> dict[str, Any]:
    if mission_kind not in {VLA_ONLY_MISSION_KIND, THREE_STAGE_MISSION_KIND}:
        raise ValueError("physical AI mission kind is not catalogued")
    resolution = resolve_physical_ai_request(operator_instruction)
    if resolution.rejection_reason or resolution.mission_kind != mission_kind:
        raise ValueError(
            "operator instruction does not resolve to the selected catalog"
        )
    if resolution.task_catalog_id != LIBERO_STOVE_MOKA_TASK_CATALOG_ID:
        raise ValueError("resolved VLA task is not catalogued")
    task_selection = _vla_task_catalog_entry_material()
    created_at = (now or datetime.now(timezone.utc)).isoformat()
    parent_run_identity = f"physical-ai-chat-run:{uuid4()}"
    if mission_kind == THREE_STAGE_MISSION_KIND:
        parent, _, children, episode_identity = build_three_stage_bundle(
            parent_run_identity=parent_run_identity,
        )
        parent_material: dict[str, Any] | None = parent.to_material()
        authority_bundle_ref = THREE_STAGE_AUTHORITY_BUNDLE_REF
        stage_refs = [PX4_STAGE_REF, NAV2_STAGE_REF, LIBERO_STAGE_REF]
    else:
        episode_identity = f"{parent_run_identity}:libero-episode-1"
        children = {
            LIBERO_STAGE_REF: build_libero_contract(
                parent_run_identity=parent_run_identity,
                episode_identity=episode_identity,
            )
        }
        parent_material = None
        authority_bundle_ref = VLA_ONLY_AUTHORITY_BUNDLE_REF
        stage_refs = [LIBERO_STAGE_REF]
    base = {
        "schema_version": PHYSICAL_AI_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": f"physical-ai-proposal:{uuid4()}",
        "mission_kind": mission_kind,
        "operator_instruction": str(operator_instruction).strip(),
        "created_at": created_at,
        "parent_run_identity": parent_run_identity,
        "episode_identity": episode_identity,
        "stage_refs": stage_refs,
        "authority_bundle_ref": authority_bundle_ref,
        "vla_task_resolution": {
            "resolver_version": VLA_TASK_RESOLVER_VERSION,
            "resolver_sha256": _vla_task_resolver_sha256(),
            "match_kind": resolution.match_kind,
            "catalog_entry_id": resolution.task_catalog_id,
            "catalog_entry_sha256": task_selection["content_sha256"],
        },
        "vla_task_selection": task_selection,
        "parent_contract": parent_material,
        "child_contracts": {
            stage_ref: contract.to_material()
            for stage_ref, contract in children.items()
        },
        "proposal_only": True,
        "human_approval_required": True,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "physical_execution_invoked": False,
        "mission_completion_claimed": False,
        "shared_world_claimed": False,
        "identity_continuity_claimed": False,
    }
    return {**base, "proposal_sha256": canonical_sha256(base)}


def validate_physical_ai_proposal(proposal: Mapping[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    if proposal.get("schema_version") != PHYSICAL_AI_PROPOSAL_SCHEMA_VERSION:
        reasons.append("physical_ai_proposal_schema_not_supported")
    if proposal.get("proposal_sha256") != proposal_sha256(proposal):
        reasons.append("physical_ai_proposal_digest_mismatch")
    mission_kind = str(proposal.get("mission_kind") or "")
    resolution = resolve_physical_ai_request(
        str(proposal.get("operator_instruction") or "")
    )
    if resolution.rejection_reason:
        reasons.append(resolution.rejection_reason)
    if resolution.mission_kind != mission_kind:
        reasons.append("physical_ai_language_mission_kind_mismatch")
    expected_task_selection = _vla_task_catalog_entry_material()
    if proposal.get("vla_task_selection") != expected_task_selection:
        reasons.append("physical_ai_vla_task_selection_mismatch")
    expected_resolution = {
        "resolver_version": VLA_TASK_RESOLVER_VERSION,
        "resolver_sha256": _vla_task_resolver_sha256(),
        "match_kind": resolution.match_kind,
        "catalog_entry_id": resolution.task_catalog_id,
        "catalog_entry_sha256": expected_task_selection["content_sha256"],
    }
    if proposal.get("vla_task_resolution") != expected_resolution:
        reasons.append("physical_ai_vla_task_resolution_mismatch")
    parent_run_identity = str(proposal.get("parent_run_identity") or "")
    if not parent_run_identity:
        reasons.append("physical_ai_parent_run_identity_missing")
        return tuple(dict.fromkeys(reasons))
    try:
        if mission_kind == THREE_STAGE_MISSION_KIND:
            parent, _, children, episode_identity = build_three_stage_bundle(
                parent_run_identity=parent_run_identity,
            )
            expected_parent: dict[str, Any] | None = parent.to_material()
            expected_refs = [PX4_STAGE_REF, NAV2_STAGE_REF, LIBERO_STAGE_REF]
        elif mission_kind == VLA_ONLY_MISSION_KIND:
            episode_identity = f"{parent_run_identity}:libero-episode-1"
            children = {
                LIBERO_STAGE_REF: build_libero_contract(
                    parent_run_identity=parent_run_identity,
                    episode_identity=episode_identity,
                )
            }
            expected_parent = None
            expected_refs = [LIBERO_STAGE_REF]
        else:
            reasons.append("physical_ai_mission_kind_not_catalogued")
            return tuple(dict.fromkeys(reasons))
    except (TypeError, ValueError):
        reasons.append("physical_ai_proposal_contract_rebuild_failed")
        return tuple(dict.fromkeys(reasons))
    if proposal.get("episode_identity") != episode_identity:
        reasons.append("physical_ai_episode_identity_mismatch")
    if proposal.get("stage_refs") != expected_refs:
        reasons.append("physical_ai_stage_order_mismatch")
    if proposal.get("parent_contract") != expected_parent:
        reasons.append("physical_ai_parent_contract_mismatch")
    expected_children = {
        stage_ref: contract.to_material()
        for stage_ref, contract in children.items()
    }
    if proposal.get("child_contracts") != expected_children:
        reasons.append("physical_ai_child_contracts_mismatch")
    for field in (
        "approval_created",
        "dispatch_authority_created",
        "runtime_effect_requested",
        "physical_execution_invoked",
        "mission_completion_claimed",
        "shared_world_claimed",
        "identity_continuity_claimed",
    ):
        if proposal.get(field) is not False:
            reasons.append(f"physical_ai_proposal_{field}_forbidden")
    return tuple(dict.fromkeys(reasons))


def build_physical_ai_approval(
    *,
    proposal: Mapping[str, Any],
    operator_approval_ref: str,
    approved_at: datetime | None = None,
) -> dict[str, Any]:
    reasons = validate_physical_ai_proposal(proposal)
    if reasons:
        raise ValueError("physical AI proposal invalid: " + ",".join(reasons))
    approval_ref = str(operator_approval_ref or "").strip()
    if not approval_ref:
        raise ValueError("operator approval ref is required")
    mission_kind = str(proposal["mission_kind"])
    parent_approval: dict[str, Any] | None = None
    if mission_kind == THREE_STAGE_MISSION_KIND:
        _, approval, _, _ = build_three_stage_bundle(
            parent_run_identity=str(proposal["parent_run_identity"]),
            operator_approval_ref=approval_ref,
        )
        if approval is None:  # pragma: no cover - guarded above
            raise ValueError("parent approval was not created")
        parent_approval = {
            **approval.to_material(),
            "approval_binding_sha256": approval.approval_binding_sha256,
        }
    base = {
        "schema_version": PHYSICAL_AI_APPROVAL_SCHEMA_VERSION,
        "approval_id": f"physical-ai-approval:{uuid4()}",
        "proposal_id": proposal["proposal_id"],
        "proposal_sha256": proposal["proposal_sha256"],
        "mission_kind": mission_kind,
        "parent_run_identity": proposal["parent_run_identity"],
        "operator_approval_ref": approval_ref,
        "authority_bundle_ref": proposal["authority_bundle_ref"],
        "vla_task_catalog_entry_id": proposal["vla_task_selection"][
            "catalog_entry_id"
        ],
        "vla_task_catalog_entry_sha256": proposal["vla_task_selection"][
            "content_sha256"
        ],
        "vla_instruction_sha256": proposal["vla_task_selection"][
            "instruction_sha256"
        ],
        "approved_at": (approved_at or datetime.now(timezone.utc)).isoformat(),
        "parent_approval": parent_approval,
        "human_operator_approval_recorded": True,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "physical_execution_invoked": False,
    }
    return {**base, "approval_sha256": canonical_sha256(base)}


def validate_physical_ai_approval(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> tuple[str, ...]:
    reasons = list(validate_physical_ai_proposal(proposal))
    if approval.get("schema_version") != PHYSICAL_AI_APPROVAL_SCHEMA_VERSION:
        reasons.append("physical_ai_approval_schema_not_supported")
    approval_material = {
        key: value for key, value in dict(approval).items() if key != "approval_sha256"
    }
    if approval.get("approval_sha256") != canonical_sha256(approval_material):
        reasons.append("physical_ai_approval_digest_mismatch")
    for field in (
        "proposal_id",
        "proposal_sha256",
        "mission_kind",
        "parent_run_identity",
        "authority_bundle_ref",
    ):
        expected = (
            proposal.get("proposal_sha256")
            if field == "proposal_sha256"
            else proposal.get(field)
        )
        if approval.get(field) != expected:
            reasons.append(f"physical_ai_approval_{field}_mismatch")
    task_selection = proposal.get("vla_task_selection")
    task_selection = (
        task_selection if isinstance(task_selection, Mapping) else {}
    )
    for field, expected in (
        ("vla_task_catalog_entry_id", task_selection.get("catalog_entry_id")),
        (
            "vla_task_catalog_entry_sha256",
            task_selection.get("content_sha256"),
        ),
        ("vla_instruction_sha256", task_selection.get("instruction_sha256")),
    ):
        if approval.get(field) != expected:
            reasons.append(f"physical_ai_approval_{field}_mismatch")
    if approval.get("human_operator_approval_recorded") is not True:
        reasons.append("physical_ai_human_approval_missing")
    for field in (
        "dispatch_authority_created",
        "runtime_effect_requested",
        "physical_execution_invoked",
    ):
        if approval.get(field) is not False:
            reasons.append(f"physical_ai_approval_{field}_forbidden")
    if proposal.get("mission_kind") == THREE_STAGE_MISSION_KIND:
        _, expected_approval, _, _ = build_three_stage_bundle(
            parent_run_identity=str(proposal.get("parent_run_identity") or ""),
            operator_approval_ref=str(approval.get("operator_approval_ref") or ""),
        )
        expected_material = (
            {
                **expected_approval.to_material(),
                "approval_binding_sha256": expected_approval.approval_binding_sha256,
            }
            if expected_approval is not None
            else None
        )
        if approval.get("parent_approval") != expected_material:
            reasons.append("physical_ai_parent_approval_binding_mismatch")
    elif approval.get("parent_approval") is not None:
        reasons.append("physical_ai_vla_only_parent_approval_forbidden")
    return tuple(dict.fromkeys(reasons))


def build_physical_ai_context(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any] | None = None,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    proposal_reasons = validate_physical_ai_proposal(proposal)
    approval_reasons = (
        validate_physical_ai_approval(proposal=proposal, approval=approval)
        if isinstance(approval, Mapping)
        else ()
    )
    summary = {
        "status": (
            "running"
            if isinstance(execution, Mapping)
            and str(execution.get("task_status") or "") in {"running", "pending"}
            else "approved"
            if isinstance(approval, Mapping) and not approval_reasons
            else "proposed"
            if not proposal_reasons
            else "blocked"
        ),
        "physical_ai_mission_kind": proposal.get("mission_kind"),
        "proposal_id": proposal.get("proposal_id"),
        "proposal_sha256": proposal.get("proposal_sha256"),
        "parent_run_identity": proposal.get("parent_run_identity"),
        "stage_refs": list(proposal.get("stage_refs") or []),
        "stage_count": len(proposal.get("stage_refs") or []),
        "selected_vla_task_catalog_id": (
            proposal.get("vla_task_selection", {}).get("catalog_entry_id")
            if isinstance(proposal.get("vla_task_selection"), Mapping)
            else None
        ),
        "resolved_vla_instruction": (
            proposal.get("vla_task_selection", {}).get("resolved_instruction")
            if isinstance(proposal.get("vla_task_selection"), Mapping)
            else None
        ),
        "vla_instruction_delivery_mode": (
            proposal.get("vla_task_selection", {}).get(
                "instruction_delivery_mode"
            )
            if isinstance(proposal.get("vla_task_selection"), Mapping)
            else None
        ),
        "policy_instruction_delivery_claimed": False,
        "approval_status": (
            "approved" if isinstance(approval, Mapping) and not approval_reasons else "pending"
        ),
        "approval_sha256": approval.get("approval_sha256") if approval else None,
        "task_id": execution.get("task_id") if execution else None,
        "task_status": execution.get("task_status") if execution else None,
        "validation_reasons": list(dict.fromkeys((*proposal_reasons, *approval_reasons))),
        "mission_completion_claimed": False,
        "physical_execution_invoked": False,
    }
    return {
        "physical_ai_mission_proposal": dict(proposal),
        "physical_ai_mission_validation": {
            "schema_version": PHYSICAL_AI_VALIDATION_SCHEMA_VERSION,
            "status": "passed" if not proposal_reasons else "blocked",
            "reasons": list(proposal_reasons),
            "dispatch_authority_created": False,
            "runtime_effect_requested": False,
            "physical_execution_invoked": False,
        },
        "physical_ai_mission_approval": dict(approval) if approval else None,
        "physical_ai_mission_execution": dict(execution) if execution else None,
        "summary": summary,
    }


__all__ = [
    "LIBERO_STOVE_MOKA_INSTRUCTION",
    "LIBERO_STOVE_MOKA_TASK_CATALOG_ID",
    "LIBERO_STAGE_REF",
    "NAV2_STAGE_REF",
    "PHYSICAL_AI_APPROVAL_SCHEMA_VERSION",
    "PHYSICAL_AI_PROPOSAL_SCHEMA_VERSION",
    "PX4_STAGE_REF",
    "THREE_STAGE_MISSION_KIND",
    "VLA_ONLY_MISSION_KIND",
    "VLA_TASK_CATALOG_SCHEMA_VERSION",
    "PhysicalAIRequestResolution",
    "approved_vla_task_catalog",
    "build_libero_contract",
    "build_physical_ai_approval",
    "build_physical_ai_context",
    "build_physical_ai_proposal",
    "build_three_stage_bundle",
    "physical_ai_request_kind",
    "resolve_physical_ai_request",
    "proposal_sha256",
    "validate_physical_ai_approval",
    "validate_physical_ai_proposal",
]
