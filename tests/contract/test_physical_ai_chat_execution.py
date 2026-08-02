from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from missionos_core import canonical_sha256

from src.runtime.physical_ai_chat_execution import (
    PHYSICAL_AI_LIBERO_STAGE_COMMAND_ENV,
    _libero_command,
    run_physical_ai_chat_execution,
)
from src.runtime.libero_panda_predicate_package import LIBERO_REVISION
from src.runtime.physical_ai_mission_catalog import (
    LIBERO_STOVE_MOKA_TASK_CATALOG_ID,
    THREE_STAGE_MISSION_KIND,
    VLA_ONLY_MISSION_KIND,
    approved_vla_task_catalog,
    build_physical_ai_approval,
    build_physical_ai_proposal,
    physical_ai_request_kind,
    resolve_physical_ai_request,
    validate_physical_ai_approval,
    validate_physical_ai_proposal,
)
from src.runtime.physical_ai_vla_capability_inventory import (
    build_vla_success_predicate_draft,
    resolve_source_backed_vla_candidate,
    vla_task_capability_inventory,
)
from src.runtime.task_store import TaskStore
from src.runtime.vla_post_episode_repair import (
    build_vla_post_episode_repair_approval,
    build_vla_post_episode_repair_proposal,
    validate_vla_post_episode_repair_approval,
    validate_vla_post_episode_repair_proposal,
)


NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _proposal_and_approval(kind: str):
    instruction = (
        "PX4、Nav2、GR00Tを一つのミッションとして統合管制して"
        if kind == THREE_STAGE_MISSION_KIND
        else "GR00TでVLAミッションを実行して"
    )
    proposal = build_physical_ai_proposal(
        operator_instruction=instruction,
        mission_kind=kind,
        now=NOW,
    )
    approval = build_physical_ai_approval(
        proposal=proposal,
        operator_approval_ref="operator:test",
        approved_at=NOW,
    )
    return proposal, approval


def _task(store: TaskStore, kind: str) -> dict:
    return store.create(
        kind=kind,
        title="fixture",
        status="running",
    )


def test_natural_language_selects_only_catalogued_physical_ai_packages() -> None:
    assert physical_ai_request_kind("GR00TでVLAミッションを実行して") == (
        VLA_ONLY_MISSION_KIND
    )
    assert physical_ai_request_kind("PX4、Nav2、GR00Tを統合管制して") == (
        THREE_STAGE_MISSION_KIND
    )
    assert physical_ai_request_kind("PX4で飛んで") is None


def test_supported_task_language_resolves_to_exact_approved_entry() -> None:
    resolution = resolve_physical_ai_request(
        "GR00Tでコンロを点けてモカポットを置いて"
    )

    assert resolution.mission_kind == VLA_ONLY_MISSION_KIND
    assert resolution.task_catalog_id == LIBERO_STOVE_MOKA_TASK_CATALOG_ID
    assert resolution.match_kind == "approved_task_alias"
    assert resolution.rejection_reason is None
    catalog = approved_vla_task_catalog()
    assert len(catalog) == 1
    assert catalog[0]["catalog_entry_id"] == resolution.task_catalog_id
    assert catalog[0]["policy_instruction_delivery_claimed"] is False


def test_unsupported_specific_vla_language_is_not_silently_substituted() -> None:
    for instruction in (
        "GR00Tで赤いカップを梱包箱に入れて",
        "GR00TでVLAミッションとして赤いカップを梱包箱に入れて",
        "GR00Tでコンロを点けてモカポットを置いてから赤いカップを運んで",
    ):
        resolution = resolve_physical_ai_request(instruction)

        assert resolution.requested is True
        assert resolution.mission_kind is None
        assert resolution.task_catalog_id is None
        assert resolution.rejection_reason == "approved_vla_task_not_found"


def test_capability_inventory_keeps_source_candidates_out_of_authority() -> None:
    inventory = vla_task_capability_inventory()

    assert inventory["checkpoint_suite"] == "libero_10"
    assert inventory["suite_task_count"] == 10
    assert inventory["approved_task_count"] == 1
    assert inventory["source_backed_live_unverified_count"] == 9
    assert inventory["free_form_skill_generation_supported"] is False
    assert inventory["approved_task"]["proposal_authority_available"] is True
    assert inventory["approved_task"]["dispatch_authority_available"] is True
    candidates = inventory["source_backed_candidates"]
    assert len(candidates) == 9
    assert all(
        candidate["selection_status"] == "source_backed_live_unverified"
        and candidate["proposal_authority_available"] is False
        and candidate["dispatch_authority_available"] is False
        for candidate in candidates
    )
    material = dict(inventory)
    digest = material.pop("content_sha256")
    assert digest == canonical_sha256(material)


def test_source_backed_candidate_builds_review_only_predicate_draft() -> None:
    instruction = "GR00Tで黒いボウルを下段の引き出しに入れて閉じる"
    candidate = resolve_source_backed_vla_candidate(instruction)
    draft = build_vla_success_predicate_draft(
        operator_instruction=instruction,
    )

    assert candidate is not None
    assert draft["draft_status"] == "source_backed_review_required"
    assert draft["candidate_id"] == candidate["candidate_id"]
    assert draft["predicate_material"]["bddl_sha256"] == (
        "5255fe54d7f25fad4dee8fa30a30033d8cb908f1708d953c91ab609264fb4fb8"
    )
    assert draft["predicate_material"]["goal_predicates"] == [
        "Close white_cabinet_1_bottom_region",
        "In akita_black_bowl_1 white_cabinet_1_bottom_region",
    ]
    assert draft["required_verification_basis"] == "deterministic"
    assert draft["derivation_kind"] == "pinned_bddl_goal_extraction"
    assert draft["libero_revision"] == LIBERO_REVISION
    assert draft["verification_route_options"] == []
    assert draft["human_review_required"] is True
    assert draft["approved_predicate_package_created"] is False
    assert draft["approval_created"] is False
    assert draft["dispatch_authority_created"] is False
    assert draft["outcome_claim_evaluated"] is False


def test_arbitrary_task_builds_only_unverified_development_draft() -> None:
    draft = build_vla_success_predicate_draft(
        operator_instruction="GR00Tでダンボール箱を組み立てて",
    )

    assert draft["draft_status"] == (
        "unverified_capability_development_required"
    )
    assert draft["candidate_id"] is None
    assert draft["predicate_material"] is None
    assert draft["predicate_material_sha256"] is None
    assert draft["required_verification_basis"] == "unverified"
    assert draft["required_observation_kinds"] == []
    assert draft["derivation_kind"] == "unverified_requirement_draft"
    assert draft["libero_revision"] is None
    assert draft["selected_verification_route_id"] is None
    routes = {
        route["route_id"]: route for route in draft["verification_route_options"]
    }
    assert set(routes) == {
        "fixture_flap_closed_limit_switch",
        "seating_force_profile",
        "simulator_constraint_event",
        "independent_missionos_vlm_observation",
    }
    assert routes["fixture_flap_closed_limit_switch"]["verification_basis"] == (
        "deterministic"
    )
    assert routes["seating_force_profile"]["verification_basis"] == (
        "deterministic"
    )
    assert routes["simulator_constraint_event"]["verification_basis"] == (
        "deterministic"
    )
    assert routes["independent_missionos_vlm_observation"][
        "verification_basis"
    ] == "model_inferred"
    assert routes["independent_missionos_vlm_observation"][
        "executor_self_report_accepted"
    ] is False
    assert draft["vla_executor_self_report_accepted"] is False
    assert draft["approved_predicate_package_created"] is False
    assert draft["dispatch_authority_created"] is False
    material = dict(draft)
    digest = material.pop("draft_sha256")
    assert digest == canonical_sha256(material)


def test_live_libero_uses_standard_stage_wrapper_unless_overridden(
    monkeypatch,
) -> None:
    monkeypatch.delenv(PHYSICAL_AI_LIBERO_STAGE_COMMAND_ENV, raising=False)
    assert _libero_command()[1] == (
        "scripts/run_libero_panda_stage_from_environment.py"
    )

    monkeypatch.setenv(
        PHYSICAL_AI_LIBERO_STAGE_COMMAND_ENV,
        '["configured-stage", "--bounded"]',
    )
    assert _libero_command() == ("configured-stage", "--bounded")


def test_proposal_and_approval_bind_exact_catalog_material() -> None:
    proposal, approval = _proposal_and_approval(THREE_STAGE_MISSION_KIND)

    assert validate_physical_ai_proposal(proposal) == ()
    assert validate_physical_ai_approval(proposal=proposal, approval=approval) == ()
    assert proposal["stage_refs"] == [
        "px4_gazebo_delivery",
        "nav2_turtlebot3_bounded_goal",
        "groot_libero_panda",
    ]
    assert proposal["approval_created"] is False
    assert proposal["dispatch_authority_created"] is False
    selection = proposal["vla_task_selection"]
    assert selection["catalog_entry_id"] == LIBERO_STOVE_MOKA_TASK_CATALOG_ID
    assert approval["vla_task_catalog_entry_sha256"] == selection["content_sha256"]
    assert approval["vla_instruction_sha256"] == selection["instruction_sha256"]


def test_task_selection_mutation_invalidates_proposal_and_approval() -> None:
    proposal, approval = _proposal_and_approval(VLA_ONLY_MISSION_KIND)
    mutated = deepcopy(proposal)
    mutated["vla_task_selection"]["resolved_instruction"] = "place cup in box"

    reasons = validate_physical_ai_proposal(mutated)
    assert "physical_ai_proposal_digest_mismatch" in reasons
    assert "physical_ai_vla_task_selection_mismatch" in reasons
    assert validate_physical_ai_approval(proposal=mutated, approval=approval)


def test_stage_mutation_invalidates_proposal_and_existing_approval() -> None:
    proposal, approval = _proposal_and_approval(THREE_STAGE_MISSION_KIND)
    mutated = deepcopy(proposal)
    mutated["stage_refs"] = list(reversed(mutated["stage_refs"]))

    assert "physical_ai_proposal_digest_mismatch" in (
        validate_physical_ai_proposal(mutated)
    )
    assert "physical_ai_stage_order_mismatch" in (
        validate_physical_ai_proposal(mutated)
    )
    assert validate_physical_ai_approval(
        proposal=mutated,
        approval=approval,
    )


def test_three_stage_fixture_runs_under_one_parent_without_parent_promotion(
    tmp_path,
) -> None:
    proposal, approval = _proposal_and_approval(THREE_STAGE_MISSION_KIND)
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = _task(store, "parent_mission_execution")

    record = run_physical_ai_chat_execution(
        proposal=proposal,
        approval=approval,
        execution_mode="fixture",
        task_store=store,
        task_id=task["task_id"],
    )

    coordinator = record["coordinator_record"]
    stored = store.get(task["task_id"])
    assert coordinator["stages_satisfied"] == 3
    assert coordinator["coordinator_status"] == "stages_satisfied"
    assert coordinator["mission_completion_claimed"] is False
    assert record["missionos_parent_coordinator_in_live_loop"] is False
    assert record["physical_execution_invoked"] is False
    assert stored is not None
    assert stored["status"] == "completed"


def test_vla_only_fixture_is_labeled_fixture_and_keeps_ack_unverified(
    tmp_path,
) -> None:
    proposal, approval = _proposal_and_approval(VLA_ONLY_MISSION_KIND)
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = _task(store, "vla_mission_execution")

    record = run_physical_ai_chat_execution(
        proposal=proposal,
        approval=approval,
        execution_mode="fixture",
        task_store=store,
        task_id=task["task_id"],
    )

    assert record["execution_mode"] == "fixture"
    assert record["predicate_evaluation"]["fixture_execution"] is True
    assert record["controller_ack_observed"] is False
    assert record["mission_completion_claimed"] is False
    assert record["physical_execution_invoked"] is False
    assert record["vla_task_selection"]["catalog_entry_id"] == (
        LIBERO_STOVE_MOKA_TASK_CATALOG_ID
    )
    assert record["policy_instruction_delivery_observed"] is False
    stored = store.get(task["task_id"])
    assert stored is not None
    recovery = stored["artifacts"]["missionos_vla_recovery_state"]
    assert recovery["recovery_status"] == "not_required"
    assert recovery["automatic_retry_allowed"] is False
    assert recovery["dispatch_authority_created"] is False


def test_vla_failure_creates_operator_review_only_without_retry_authority(
    tmp_path,
    monkeypatch,
) -> None:
    proposal, approval = _proposal_and_approval(VLA_ONLY_MISSION_KIND)
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = _task(store, "vla_mission_execution")

    def fail_runner(**_kwargs):
        raise RuntimeError("fixture_failure_without_secret")

    monkeypatch.setattr(
        "scripts.smoke_parent_mission_px4_nav2_libero_live._run_libero_child",
        fail_runner,
    )

    try:
        run_physical_ai_chat_execution(
            proposal=proposal,
            approval=approval,
            execution_mode="live",
            task_store=store,
            task_id=task["task_id"],
        )
    except RuntimeError:
        pass
    else:  # pragma: no cover - regression guard
        raise AssertionError("forced runner failure was not raised")

    stored = store.get(task["task_id"])
    assert stored is not None
    recovery = stored["artifacts"]["missionos_vla_recovery_state"]
    assert recovery["recovery_status"] == "operator_review_required"
    assert recovery["proposal_status"] == "awaiting_operator_approval"
    assert recovery["automatic_retry_allowed"] is False
    assert recovery["retry_requires_new_human_approval"] is True
    assert recovery["approval_created"] is False
    assert recovery["dispatch_authority_created"] is False
    assert recovery["runtime_effect_requested"] is False
    assert recovery["physical_execution_invoked"] is False
    repair = stored["artifacts"][
        "missionos_vla_post_episode_repair_last_proposal"
    ]
    assert repair["repair_action"] == "retry_same_frozen_task"
    assert repair["attempt_index"] == 1
    assert repair["maximum_retry_attempts"] == 1
    assert repair["model_inference_invoked"] is False
    assert repair["automatic_retry_allowed"] is False


def test_vla_post_episode_repair_binds_failure_and_requires_new_approval(
    tmp_path,
) -> None:
    proposal, approval = _proposal_and_approval(VLA_ONLY_MISSION_KIND)
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="vla_mission_execution",
        title="failed VLA episode",
        status="failed",
        metadata={"vla_post_episode_repair_attempt_index": 0},
    )
    failure = {
        "bounded_outcome_claimed": False,
        "mission_completion_claimed": False,
        "physical_execution_invoked": False,
    }

    repair = build_vla_post_episode_repair_proposal(
        source_task=task,
        source_proposal=proposal,
        source_approval=approval,
        failure_evidence=failure,
        now=NOW,
    )

    assert repair is not None
    assert validate_vla_post_episode_repair_proposal(
        repair,
        source_task=task,
        source_proposal=proposal,
        source_approval=approval,
        failure_evidence=failure,
    ) == ()
    assert repair["requires_new_human_approval"] is True
    assert repair["new_run_identity_required"] is True
    assert repair["new_episode_identity_required"] is True
    assert repair["new_contract_required"] is True
    assert repair["dispatch_authority_created"] is False

    repair_approval = build_vla_post_episode_repair_approval(
        repair_proposal=repair,
        operator_approval_ref="operator:repair-review",
        approved_at=NOW,
    )
    assert validate_vla_post_episode_repair_approval(
        repair_approval,
        repair_proposal=repair,
    ) == ()

    mutated_failure = {**failure, "bounded_outcome_claimed": True}
    assert "vla_post_episode_repair_failure_evidence_mismatch" in (
        validate_vla_post_episode_repair_proposal(
            repair,
            source_task=task,
            source_proposal=proposal,
            source_approval=approval,
            failure_evidence=mutated_failure,
        )
    )


def test_vla_post_episode_repair_stops_after_one_attempt(tmp_path) -> None:
    proposal, approval = _proposal_and_approval(VLA_ONLY_MISSION_KIND)
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="vla_mission_execution",
        title="failed retry episode",
        status="failed",
        metadata={"vla_post_episode_repair_attempt_index": 1},
    )

    assert build_vla_post_episode_repair_proposal(
        source_task=task,
        source_proposal=proposal,
        source_approval=approval,
        failure_evidence={"bounded_outcome_claimed": False},
        now=NOW,
    ) is None


def test_failed_vla_retry_records_limit_without_second_proposal(
    tmp_path,
    monkeypatch,
) -> None:
    proposal, approval = _proposal_and_approval(VLA_ONLY_MISSION_KIND)
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="vla_mission_execution",
        title="failed retry episode",
        status="running",
        artifacts={
            "physical_ai_mission_proposal": proposal,
            "physical_ai_mission_approval": approval,
        },
        metadata={"vla_post_episode_repair_attempt_index": 1},
    )

    def fail_runner(**_kwargs):
        raise RuntimeError("retry_episode_failed")

    monkeypatch.setattr(
        "scripts.smoke_parent_mission_px4_nav2_libero_live._run_libero_child",
        fail_runner,
    )
    with pytest.raises(RuntimeError, match="retry_episode_failed"):
        run_physical_ai_chat_execution(
            proposal=proposal,
            approval=approval,
            execution_mode="live",
            task_store=store,
            task_id=task["task_id"],
        )

    stored = store.get(task["task_id"])
    assert stored is not None
    assert stored["artifacts"]["missionos_vla_recovery_state"][
        "recovery_status"
    ] == "retry_limit_reached"
    assert "missionos_vla_post_episode_repair_last_proposal" not in (
        stored["artifacts"]
    )


def test_vla_post_episode_repair_rejects_non_failed_source(tmp_path) -> None:
    proposal, approval = _proposal_and_approval(VLA_ONLY_MISSION_KIND)
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="vla_mission_execution",
        title="running VLA episode",
        status="running",
    )

    with pytest.raises(
        ValueError,
        match="vla_post_episode_repair_source_status_invalid",
    ):
        build_vla_post_episode_repair_proposal(
            source_task=task,
            source_proposal=proposal,
            source_approval=approval,
            failure_evidence={"bounded_outcome_claimed": False},
            now=NOW,
        )
