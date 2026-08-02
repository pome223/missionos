"""Source-backed VLA task candidates that do not create runtime authority."""

from __future__ import annotations

from typing import Any

from missionos_core import canonical_sha256

from src.runtime.libero_panda_predicate_package import (
    GROOT_CHECKPOINT_REPOSITORY,
    GROOT_CHECKPOINT_REVISION,
    LIBERO_REVISION,
)
from src.runtime.physical_ai_mission_catalog import approved_vla_task_catalog


VLA_TASK_CAPABILITY_INVENTORY_SCHEMA_VERSION = (
    "missionos_vla_task_capability_inventory.v1"
)

_ARBITRARY_TASK_VERIFICATION_ROUTE_OPTIONS = (
    {
        "route_id": "fixture_flap_closed_limit_switch",
        "verification_basis": "deterministic",
        "required_observation_kinds": [
            "fixture_flap_closed_limit_switch_binary_observation"
        ],
        "applicability": (
            "A task-specific fixture exposes a binary observation for every "
            "required flap being in its approved closed position."
        ),
        "implementation_status": "not_implemented",
    },
    {
        "route_id": "seating_force_profile",
        "verification_basis": "deterministic",
        "required_observation_kinds": [
            "task_specific_seating_force_profile_observation"
        ],
        "applicability": (
            "A source-backed force or torque profile distinguishes an approved "
            "seated assembly from an incomplete assembly."
        ),
        "implementation_status": "not_implemented",
    },
    {
        "route_id": "simulator_constraint_event",
        "verification_basis": "deterministic",
        "required_observation_kinds": [
            "task_specific_simulator_constraint_event"
        ],
        "applicability": (
            "A pinned simulator emits a content-bound constraint event for the "
            "approved task, analogous to the existing Gazebo detachable-joint "
            "evidence path."
        ),
        "implementation_status": "not_implemented",
    },
    {
        "route_id": "independent_missionos_vlm_observation",
        "verification_basis": "model_inferred",
        "required_observation_kinds": [
            "perception_mission_observation"
        ],
        "applicability": (
            "A MissionOS-side VLM observes the outcome independently of the VLA "
            "executor and preserves model-inferred provenance."
        ),
        "executor_self_report_accepted": False,
        "implementation_status": "existing_observation_path_requires_task_binding",
    },
)

# The pinned LIBERO suite contains these tasks and the published ``libero_10``
# sub-checkpoint targets that suite. They remain discovery material until an
# exact MissionOS child contract and bounded live run exist for each task.
_LIBERO_10_SOURCE_BACKED_CANDIDATES = (
    (
        "KITCHEN_SCENE4_put_the_black_bowl_in_the_bottom_drawer_of_the_"
        "cabinet_and_close_it",
        "put the black bowl in the bottom drawer of the cabinet and close it",
        "黒いボウルを下段の引き出しに入れて閉じる",
        "5255fe54d7f25fad4dee8fa30a30033d8cb908f1708d953c91ab609264fb4fb8",
        (
            "Close white_cabinet_1_bottom_region",
            "In akita_black_bowl_1 white_cabinet_1_bottom_region",
        ),
    ),
    (
        "KITCHEN_SCENE6_put_the_yellow_and_white_mug_in_the_microwave_"
        "and_close_it",
        "put the yellow and white mug in the microwave and close it",
        "黄白のマグを電子レンジに入れて閉じる",
        "456d145f92be049f445fc77673dc583d9d17ea7afe92f6ffcb2fd1fa5565d420",
        (
            "In white_yellow_mug_1 microwave_1_heating_region",
            "Close microwave_1",
        ),
    ),
    (
        "KITCHEN_SCENE8_put_both_moka_pots_on_the_stove",
        "put both moka pots on the stove",
        "2つのモカポットをコンロに置く",
        "ae1299a707d3e810096ad648948c517de40474bcdacca93a15e17d191e34454a",
        (
            "On moka_pot_1 flat_stove_1_cook_region",
            "On moka_pot_2 flat_stove_1_cook_region",
            "Turnon flat_stove_1",
        ),
    ),
    (
        "LIVING_ROOM_SCENE1_put_both_the_alphabet_soup_and_the_cream_"
        "cheese_box_in_the_basket",
        "put both the alphabet soup and the cream cheese box in the basket",
        "スープ缶とクリームチーズ箱をバスケットに入れる",
        "e27ab37f512fe42e35771d6130f531e6a9656929e7b4c60b348b81360cf5675d",
        (
            "In alphabet_soup_1 basket_1_contain_region",
            "In cream_cheese_1 basket_1_contain_region",
        ),
    ),
    (
        "LIVING_ROOM_SCENE2_put_both_the_alphabet_soup_and_the_tomato_"
        "sauce_in_the_basket",
        "put both the alphabet soup and the tomato sauce in the basket",
        "スープ缶とトマトソースをバスケットに入れる",
        "2e661058917f683a25bce480015197f0a2c1911bcbc2b9dd9d947199069c9618",
        (
            "In alphabet_soup_1 basket_1_contain_region",
            "In tomato_sauce_1 basket_1_contain_region",
        ),
    ),
    (
        "LIVING_ROOM_SCENE2_put_both_the_cream_cheese_box_and_the_"
        "butter_in_the_basket",
        "put both the cream cheese box and the butter in the basket",
        "クリームチーズ箱とバターをバスケットに入れる",
        "3f552805c7ab34c44debcf38a48b8131d6fee3011dd86c039650b84e0f77d058",
        (
            "In cream_cheese_1 basket_1_contain_region",
            "In butter_1 basket_1_contain_region",
        ),
    ),
    (
        "LIVING_ROOM_SCENE5_put_the_white_mug_on_the_left_plate_and_put_"
        "the_yellow_and_white_mug_on_the_right_plate",
        "put the white mug on the left plate and put the yellow and white mug "
        "on the right plate",
        "白いマグを左皿、黄白のマグを右皿に置く",
        "0c6749b920bff1e1efe47ffb0e0b801a3e6959a650e710f376dd2b812c74e865",
        (
            "On porcelain_mug_1 plate_1",
            "On white_yellow_mug_1 plate_2",
        ),
    ),
    (
        "LIVING_ROOM_SCENE6_put_the_white_mug_on_the_plate_and_put_the_"
        "chocolate_pudding_to_the_right_of_the_plate",
        "put the white mug on the plate and put the chocolate pudding to the "
        "right of the plate",
        "白いマグを皿に置き、プリンを皿の右に置く",
        "dbc9464b424cdc9b771b92d0b8a8c185b1b539f6a4133b323dc858bcf8f6a9c2",
        (
            "On porcelain_mug_1 plate_1",
            "On chocolate_pudding_1 living_room_table_plate_right_region",
        ),
    ),
    (
        "STUDY_SCENE1_pick_up_the_book_and_place_it_in_the_back_"
        "compartment_of_the_caddy",
        "pick up the book and place it in the back compartment of the caddy",
        "本をキャディーの後部区画に入れる",
        "07ca32c940d70c065e870452ef9c63e6dfa1d8506e6a86cf9b3763f652ba2d9f",
        ("In black_book_1 desk_caddy_1_back_contain_region",),
    ),
)


def vla_task_capability_inventory() -> dict[str, Any]:
    """Expose source-backed candidates without promoting them to approval."""

    approved = approved_vla_task_catalog()[0]
    candidates = [
        {
            "candidate_id": f"libero-10:{task_id}",
            "display_name_ja": display_name_ja,
            "resolved_instruction": instruction,
            "environment": f"libero_sim/{task_id}",
            "bddl_sha256": bddl_sha256,
            "goal_predicates": list(goal_predicates),
            "goal_combination": "logical_conjunction",
            "checkpoint_suite": "libero_10",
            "checkpoint_repository": GROOT_CHECKPOINT_REPOSITORY,
            "checkpoint_revision": GROOT_CHECKPOINT_REVISION,
            "source_revision": LIBERO_REVISION,
            "selection_status": "source_backed_live_unverified",
            "proposal_authority_available": False,
            "dispatch_authority_available": False,
        }
        for task_id, instruction, display_name_ja, bddl_sha256, goal_predicates in (
            _LIBERO_10_SOURCE_BACKED_CANDIDATES
        )
    ]
    base = {
        "schema_version": VLA_TASK_CAPABILITY_INVENTORY_SCHEMA_VERSION,
        "checkpoint_suite": "libero_10",
        "suite_task_count": 10,
        "approved_task_count": 1,
        "source_backed_live_unverified_count": len(candidates),
        "approved_task": {
            "catalog_entry_id": approved["catalog_entry_id"],
            "display_name_ja": approved["display_name_ja"],
            "resolved_instruction": approved["resolved_instruction"],
            "environment": approved["environment"],
            "selection_status": "approved_live_verified",
            "proposal_authority_available": True,
            "dispatch_authority_available": True,
            "content_sha256": approved["content_sha256"],
        },
        "source_backed_candidates": candidates,
        "free_form_skill_generation_supported": False,
        "new_skill_requirements": [
            "compatible embodiment and tool hardware",
            "task environment and observations",
            "task-specific demonstration or post-training data",
            "compatible checkpoint and controller path",
            "deterministic or explicitly scoped success predicate",
            "bounded live validation before catalog approval",
        ],
    }
    return {**base, "content_sha256": canonical_sha256(base)}


def _normalized_instruction(text: str) -> str:
    normalized = " ".join(
        str(text or "").casefold().replace("　", " ").split()
    )
    return normalized.rstrip("。.!?？")


def _candidate_aliases(candidate: dict[str, Any]) -> frozenset[str]:
    instruction = str(candidate["resolved_instruction"])
    display_name_ja = str(candidate["display_name_ja"])
    return frozenset(
        _normalized_instruction(alias)
        for alias in (
            instruction,
            f"gr00t: {instruction}",
            f"use gr00t to {instruction}",
            display_name_ja,
            f"GR00Tで{display_name_ja}",
        )
    )


def resolve_source_backed_vla_candidate(
    operator_instruction: str,
) -> dict[str, Any] | None:
    """Resolve an exact source-backed candidate without granting authority."""

    normalized = _normalized_instruction(operator_instruction)
    for candidate in vla_task_capability_inventory()[
        "source_backed_candidates"
    ]:
        if normalized in _candidate_aliases(candidate):
            return dict(candidate)
    return None


def build_vla_success_predicate_draft(
    *,
    operator_instruction: str,
) -> dict[str, Any]:
    """Build a review-only success-predicate draft for MissionOS chat.

    A pinned BDDL candidate produces a source-backed draft. Arbitrary language
    produces only an unverified capability-development draft. Neither form is
    an approved predicate package and neither creates runtime authority.
    """

    requested = str(operator_instruction or "").strip()
    candidate = resolve_source_backed_vla_candidate(requested)
    inventory = vla_task_capability_inventory()
    if candidate is not None:
        predicate_material = {
            "source_revision": candidate["source_revision"],
            "environment": candidate["environment"],
            "bddl_sha256": candidate["bddl_sha256"],
            "goal_predicates": candidate["goal_predicates"],
            "combination": candidate["goal_combination"],
            "scope": (
                "one exact pinned LIBERO simulator episode; not a real-world "
                "semantic-completion claim"
            ),
        }
        draft_status = "source_backed_review_required"
        required_basis = "deterministic"
        missing_requirements = [
            "approved task-specific MissionOS predicate package",
            "bounded live validation for this exact environment",
        ]
        outcome_statement = (
            "The exact pinned LIBERO simulator episode satisfied the pinned "
            f"task predicate for: {candidate['resolved_instruction']}."
        )
        derivation_kind = "pinned_bddl_goal_extraction"
        libero_revision = candidate["source_revision"]
        verification_route_options: list[dict[str, Any]] = []
    else:
        predicate_material = None
        draft_status = "unverified_capability_development_required"
        required_basis = "unverified"
        missing_requirements = list(inventory["new_skill_requirements"])
        outcome_statement = (
            "Requested outcome requires an observable, source-backed predicate: "
            f"{requested}."
        )
        derivation_kind = "unverified_requirement_draft"
        libero_revision = None
        verification_route_options = [
            dict(route) for route in _ARBITRARY_TASK_VERIFICATION_ROUTE_OPTIONS
        ]
    base = {
        "schema_version": "missionos_vla_success_predicate_draft.v1",
        "operator_instruction": requested,
        "draft_status": draft_status,
        "derivation_kind": derivation_kind,
        "candidate_id": candidate["candidate_id"] if candidate else None,
        "libero_revision": libero_revision,
        "outcome_claim_spec": {
            "statement": outcome_statement,
            "claim_scope": (
                "exact_pinned_libero_simulator_episode"
                if candidate
                else "unresolved_requested_task"
            ),
        },
        "predicate_material": predicate_material,
        "predicate_material_sha256": (
            canonical_sha256(predicate_material) if predicate_material else None
        ),
        "required_observation_kinds": (
            [
                "content_bound_simulator_step_returns",
                "same_episode_official_predicate_result",
            ]
            if candidate
            else []
        ),
        "verification_route_options": verification_route_options,
        "selected_verification_route_id": None,
        "vla_executor_self_report_accepted": False,
        "required_verification_basis": required_basis,
        "missing_requirements": missing_requirements,
        "human_review_required": True,
        "approved_predicate_package_created": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "outcome_claim_evaluated": False,
        "mission_completion_claimed": False,
        "physical_execution_invoked": False,
    }
    return {**base, "draft_sha256": canonical_sha256(base)}


__all__ = [
    "VLA_TASK_CAPABILITY_INVENTORY_SCHEMA_VERSION",
    "build_vla_success_predicate_draft",
    "resolve_source_backed_vla_candidate",
    "vla_task_capability_inventory",
]
