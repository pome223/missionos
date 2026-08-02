"""Task-backed execution boundary for approved Physical AI chat missions.

The chat surface selects and approves an exact catalog package.  This module
owns task lifecycle and coordinator sequencing; executor-specific logic stays
in the existing opt-in simulator adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any

from missionos_core import FrozenMissionContract, canonical_sha256

from src.runtime.parent_mission_coordinator import run_parent_mission_coordinator
from src.runtime.physical_ai_mission_catalog import (
    LIBERO_STAGE_REF,
    NAV2_STAGE_REF,
    PX4_STAGE_REF,
    THREE_STAGE_MISSION_KIND,
    VLA_ONLY_MISSION_KIND,
    build_libero_contract,
    build_three_stage_bundle,
    validate_physical_ai_approval,
)
from src.runtime.vla_post_episode_repair import (
    build_vla_post_episode_repair_proposal,
)
from src.runtime.task_store import TaskStore, get_task_store


PHYSICAL_AI_CHAT_EXECUTION_OPT_IN_ENV = (
    "RUN_MISSIONOS_PHYSICAL_AI_CHAT_EXECUTION"
)
PHYSICAL_AI_CHAT_EXECUTION_MODE_ENV = "MISSIONOS_PHYSICAL_AI_CHAT_EXECUTION_MODE"
PHYSICAL_AI_CHAT_FIXTURE_OPT_IN_ENV = "RUN_MISSIONOS_PHYSICAL_AI_CHAT_FIXTURE"
PHYSICAL_AI_CHAT_ARTIFACT_ROOT_ENV = "MISSIONOS_PHYSICAL_AI_CHAT_ARTIFACT_ROOT"
PHYSICAL_AI_LIBERO_STAGE_COMMAND_ENV = "MISSIONOS_LIBERO_LIVE_STAGE_COMMAND_JSON"


def build_vla_recovery_state(
    status: str,
    *,
    repair_proposal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {
        "monitoring",
        "not_required",
        "operator_review_required",
        "retry_dispatched",
        "retry_dispatch_failed",
        "retry_limit_reached",
    }:
        raise ValueError("vla_recovery_status_invalid")
    needs_review = status == "operator_review_required"
    retry_dispatched = status == "retry_dispatched"
    retry_dispatch_failed = status == "retry_dispatch_failed"
    proposal = repair_proposal if isinstance(repair_proposal, Mapping) else {}
    return {
        "schema_version": "missionos_vla_recovery_state.v1",
        "recovery_status": status,
        "proposal_status": (
            str(proposal.get("proposal_status") or "proposal_only")
            if needs_review or retry_dispatched or retry_dispatch_failed
            else "none"
        ),
        "repair_action": (
            str(proposal.get("repair_action") or "unknown")
            if needs_review or retry_dispatched or retry_dispatch_failed
            else "none"
        ),
        "repair_proposal_id": (
            str(proposal.get("proposal_id") or "")
            if needs_review or retry_dispatched or retry_dispatch_failed
            else ""
        ),
        "repair_proposal_sha256": (
            str(proposal.get("proposal_sha256") or "")
            if needs_review or retry_dispatched or retry_dispatch_failed
            else ""
        ),
        "recommended_action": (
            "inspect_failure_receipt_then_operator_review"
            if needs_review
            else "monitor_new_repair_task"
            if retry_dispatched
            else "inspect_retry_dispatch_failure"
            if retry_dispatch_failed
            else "none"
        ),
        "automatic_retry_allowed": False,
        "retry_requires_new_human_approval": True,
        "external_stop_available": False,
        "in_episode_intervention_available": False,
        "in_episode_intervention_unavailable_reason": (
            "official_runner_has_no_external_stop_or_runtime_callback"
        ),
        "post_episode_repair_implemented": True,
        "post_episode_repair_proposal_created": needs_review or retry_dispatched,
        "repair_planner_invoked": proposal.get("planner_invoked") is True,
        "repair_model_inference_invoked": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "This state may expose one post-episode repair proposal. It cannot "
            "intervene in the completed episode, retry without new human approval, "
            "or claim a safe-stop effect."
        ),
    }


def _vla_failure_recovery_artifacts(
    *,
    task_store: TaskStore,
    task_id: str,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    failure_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    stored = task_store.get(task_id) or {"task_id": task_id}
    failed_source = {**stored, "status": "failed"}
    repair_proposal = build_vla_post_episode_repair_proposal(
        source_task=failed_source,
        source_proposal=proposal,
        source_approval=approval,
        failure_evidence=failure_evidence,
    )
    if repair_proposal is None:
        return {
            "missionos_vla_recovery_state": build_vla_recovery_state(
                "retry_limit_reached"
            )
        }
    proposal_id = str(repair_proposal["proposal_id"])
    return {
        "missionos_vla_post_episode_repair_proposals": {
            proposal_id: repair_proposal,
        },
        "missionos_vla_post_episode_repair_last_proposal": repair_proposal,
        "missionos_vla_recovery_state": build_vla_recovery_state(
            "operator_review_required",
            repair_proposal=repair_proposal,
        ),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def physical_ai_chat_execution_mode() -> str:
    if os.environ.get(PHYSICAL_AI_CHAT_EXECUTION_OPT_IN_ENV) != "1":
        return "disabled"
    mode = os.environ.get(PHYSICAL_AI_CHAT_EXECUTION_MODE_ENV, "live").strip().lower()
    if mode not in {"fixture", "live"}:
        return "invalid"
    if mode == "fixture" and os.environ.get(PHYSICAL_AI_CHAT_FIXTURE_OPT_IN_ENV) != "1":
        return "fixture_not_opted_in"
    return mode


def _fixture_evaluation(
    contract: FrozenMissionContract,
    *,
    stage_ref: str,
) -> dict[str, Any]:
    """Return explicit fixture evidence; never represent it as a live run."""

    content_sha256 = canonical_sha256(
        {
            "fixture": "physical_ai_chat_execution",
            "stage_ref": stage_ref,
            "contract_sha256": contract.contract_sha256,
        }
    )
    return {
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
        "predicate_package_id": contract.predicate_package.package_id,
        "predicate_package_version": contract.predicate_package.package_version,
        "predicate_package_sha256": contract.predicate_package.content_sha256,
        "status": "satisfied",
        "evaluated_outcome_claim": True,
        "actual_verification_basis": "deterministic",
        "predicate_package_evaluated": True,
        "evidence_readiness": "ready",
        "observation_content_sha256": content_sha256,
        "evidence_origins": ["stored_artifact"],
        "outcome_claim_scope": contract.outcome_claim_spec.claim_scope,
        "fixture_execution": True,
        "simulator_execution_observed": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "runtime_effect_requested": False,
        "operational_closure_created": False,
        "physical_execution_invoked": False,
    }


def _parent_record(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    coordinator: Mapping[str, Any],
    execution_mode: str,
) -> dict[str, Any]:
    parent_contract = proposal.get("parent_contract")
    parent_contract = parent_contract if isinstance(parent_contract, Mapping) else {}
    parent_approval = approval.get("parent_approval")
    parent_approval = parent_approval if isinstance(parent_approval, Mapping) else {}
    return {
        "schema_version": "missionos_physical_ai_chat_parent_run.v1",
        "parent_run_identity": proposal.get("parent_run_identity"),
        "parent_contract_frozen_at": proposal.get("created_at"),
        "parent_mission_sha256": parent_contract.get("parent_mission_sha256"),
        "approval_binding_sha256": parent_approval.get("approval_binding_sha256"),
        "shared_target_descriptor_sha256": parent_contract.get(
            "shared_target_descriptor_sha256"
        ),
        "vla_task_selection": dict(proposal.get("vla_task_selection") or {}),
        "policy_instruction_delivery_observed": False,
        "execution_mode": execution_mode,
        "coordinator_record": dict(coordinator),
        "missionos_parent_coordinator_in_live_loop": execution_mode == "live",
        "mission_completion_claimed": False,
        "identity_continuity_claimed": False,
        "shared_world_claimed": False,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "This record tracks ordered simulator stages under one approved "
            "parent authority. Stage satisfaction does not establish parent "
            "mission completion, a shared world, physical identity continuity, "
            "or physical execution."
        ),
    }


def _task_artifacts(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    parent_record: Mapping[str, Any] | None = None,
    vla_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifacts: dict[str, Any] = {
        "physical_ai_mission_proposal": dict(proposal),
        "physical_ai_mission_approval": dict(approval),
        "summary": {
            "physical_ai_mission_kind": proposal.get("mission_kind"),
            "parent_run_identity": proposal.get("parent_run_identity"),
            "proposal_sha256": proposal.get("proposal_sha256"),
            "approval_sha256": approval.get("approval_sha256"),
            "selected_vla_task_catalog_id": (
                proposal.get("vla_task_selection", {}).get("catalog_entry_id")
                if isinstance(proposal.get("vla_task_selection"), Mapping)
                else None
            ),
            "policy_instruction_delivery_observed": False,
            "mission_completion_claimed": False,
            "physical_execution_invoked": False,
        },
    }
    if parent_record is not None:
        artifacts["missionos_parent_mission_run_record"] = dict(parent_record)
    if vla_record is not None:
        artifacts["missionos_vla_mission_run_record"] = dict(vla_record)
    return artifacts


def _libero_command() -> tuple[str, ...]:
    raw = os.environ.get(PHYSICAL_AI_LIBERO_STAGE_COMMAND_ENV, "")
    if not raw.strip():
        return (
            sys.executable,
            "scripts/run_libero_panda_stage_from_environment.py",
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("libero_stage_command_json_invalid") from exc
    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError("libero_stage_command_missing")
    command = tuple(str(item) for item in parsed)
    if any(not item.strip() for item in command):
        raise RuntimeError("libero_stage_command_argument_invalid")
    return command


def _live_parent_runners(
    *,
    proposal: Mapping[str, Any],
    children: Mapping[str, FrozenMissionContract],
    run_dir: Path,
) -> Mapping[str, Any]:
    """Bind existing live adapters to the already-frozen chat contracts."""

    from scripts.smoke_parent_mission_px4_nav2_libero_live import (
        _run_libero_child,
    )
    from scripts.smoke_parent_mission_px4_nav2_live import _run_child

    root_dir = Path(__file__).resolve().parents[2]
    stage_audits: dict[str, dict[str, Any]] = {}
    common_pythonpath = os.pathsep.join(
        (
            str(root_dir),
            str(root_dir / "packages/missionos-core/src"),
            os.environ.get("PYTHONPATH", ""),
        )
    )
    px4_root = run_dir / "px4"
    nav2_root = run_dir / "nav2"
    px4_root.mkdir(parents=True)
    nav2_root.mkdir(parents=True)

    def run_px4() -> dict[str, Any]:
        return _run_child(
            stage_ref=PX4_STAGE_REF,
            command=(sys.executable, "scripts/smoke_px4_gazebo_sitl_e2e_delivery.py"),
            env={
                "RUN_PX4_GAZEBO_SITL_E2E_DELIVERY_SMOKE": "1",
                "PX4_GAZEBO_SITL_E2E_ARTIFACT_ROOT": str(px4_root),
                "PYTHONPATH": common_pythonpath,
            },
            artifact_root=px4_root,
            artifact_pattern="sitl_e2e_delivery_*",
            expected_contract=children[PX4_STAGE_REF],
            stage_audits=stage_audits,
            subprocess_runner=subprocess.run,
        )

    def run_nav2() -> dict[str, Any]:
        return _run_child(
            stage_ref=NAV2_STAGE_REF,
            command=(
                "bash",
                "scripts/smoke_ros2_nav2_turtlebot3_mission_contract_docker.sh",
            ),
            env={
                "ROS2_NAV2_MISSION_CONTRACT_ARTIFACT_ROOT": str(nav2_root),
                "PYTHONPATH": common_pythonpath,
            },
            artifact_root=nav2_root,
            artifact_pattern="nav2_turtlebot3_*",
            expected_contract=children[NAV2_STAGE_REF],
            stage_audits=stage_audits,
            subprocess_runner=subprocess.run,
        )

    def run_libero() -> dict[str, Any]:
        return _run_libero_child(
            command=_libero_command(),
            run_dir=run_dir,
            parent_run_identity=str(proposal["parent_run_identity"]),
            episode_identity=str(proposal["episode_identity"]),
            expected_contract=children[LIBERO_STAGE_REF],
            stage_audits=stage_audits,
            subprocess_runner=subprocess.run,
        )

    return {
        PX4_STAGE_REF: run_px4,
        NAV2_STAGE_REF: run_nav2,
        LIBERO_STAGE_REF: run_libero,
    }


def run_physical_ai_chat_execution(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    execution_mode: str,
    task_store: TaskStore,
    task_id: str,
) -> dict[str, Any]:
    """Run one approved catalog mission and persist truthful task evidence."""

    reasons = validate_physical_ai_approval(proposal=proposal, approval=approval)
    if reasons:
        raise RuntimeError("physical_ai_approval_invalid:" + ",".join(reasons))
    if execution_mode not in {"fixture", "live"}:
        raise RuntimeError("physical_ai_execution_mode_invalid")

    mission_kind = str(proposal.get("mission_kind") or "")
    try:
        if mission_kind == THREE_STAGE_MISSION_KIND:
            parent, parent_approval, children, _ = build_three_stage_bundle(
                parent_run_identity=str(proposal["parent_run_identity"]),
                operator_approval_ref=str(approval["operator_approval_ref"]),
            )
            if parent_approval is None:  # pragma: no cover - approval ref is validated
                raise RuntimeError("physical_ai_parent_approval_missing")

            def on_progress(snapshot: Mapping[str, Any]) -> None:
                record = _parent_record(
                    proposal=proposal,
                    approval=approval,
                    coordinator=snapshot,
                    execution_mode=execution_mode,
                )
                task_store.update(
                    task_id,
                    status="running",
                    replace_artifacts={
                        "missionos_parent_mission_run_record": record,
                    },
                )

            if execution_mode == "fixture":
                runners = {
                    stage.stage_ref: (
                        lambda contract=children[stage.stage_ref], ref=stage.stage_ref: (
                            _fixture_evaluation(contract, stage_ref=ref)
                        )
                    )
                    for stage in parent.stages
                }
            else:
                artifact_root = Path(
                    os.environ.get(
                        PHYSICAL_AI_CHAT_ARTIFACT_ROOT_ENV,
                        "artifacts/physical_ai_chat",
                    )
                )
                run_dir = artifact_root / str(proposal["parent_run_identity"]).replace(
                    ":", "_"
                )
                run_dir.mkdir(parents=True, exist_ok=False)
                runners = _live_parent_runners(
                    proposal=proposal,
                    children=children,
                    run_dir=run_dir,
                )
            coordinator = run_parent_mission_coordinator(
                contract=parent,
                approval=parent_approval,
                stage_runners=runners,
                progress_callback=on_progress,
            )
            parent_record = _parent_record(
                proposal=proposal,
                approval=approval,
                coordinator=coordinator,
                execution_mode=execution_mode,
            )
            final_status = (
                "completed"
                if coordinator.get("coordinator_status") == "stages_satisfied"
                else "failed"
            )
            task_store.update(
                task_id,
                status=final_status,
                replace_artifacts={
                    "missionos_parent_mission_run_record": parent_record,
                },
            )
            return parent_record

        if mission_kind != VLA_ONLY_MISSION_KIND:
            raise RuntimeError("physical_ai_mission_kind_not_catalogued")
        contract = build_libero_contract(
            parent_run_identity=str(proposal["parent_run_identity"]),
            episode_identity=str(proposal["episode_identity"]),
        )
        if execution_mode == "fixture":
            evaluation = _fixture_evaluation(contract, stage_ref=LIBERO_STAGE_REF)
        else:
            from scripts.smoke_parent_mission_px4_nav2_libero_live import (
                _run_libero_child,
            )

            artifact_root = Path(
                os.environ.get(
                    PHYSICAL_AI_CHAT_ARTIFACT_ROOT_ENV,
                    "artifacts/physical_ai_chat",
                )
            )
            run_dir = artifact_root / str(proposal["parent_run_identity"]).replace(
                ":", "_"
            )
            run_dir.mkdir(parents=True, exist_ok=False)
            evaluation = _run_libero_child(
                command=_libero_command(),
                run_dir=run_dir,
                parent_run_identity=str(proposal["parent_run_identity"]),
                episode_identity=str(proposal["episode_identity"]),
                expected_contract=contract,
                stage_audits={},
                subprocess_runner=subprocess.run,
            )
        vla_record = {
            "schema_version": "missionos_vla_mission_run_record.v1",
            "mission_kind": mission_kind,
            "run_identity": proposal.get("parent_run_identity"),
            "episode_identity": proposal.get("episode_identity"),
            "contract_sha256": contract.contract_sha256,
            "vla_task_selection": dict(
                proposal.get("vla_task_selection") or {}
            ),
            "policy_instruction_delivery_observed": False,
            "execution_mode": execution_mode,
            "predicate_evaluation": evaluation,
            "bounded_outcome_claimed": evaluation.get("evaluated_outcome_claim") is True,
            "controller_ack_observed": False,
            "mission_completion_claimed": False,
            "physical_execution_invoked": False,
            "claim_boundary": (
                "The exact LIBERO simulator episode predicate may be satisfied. "
                "This does not establish an independent controller ACK, parent "
                "mission completion, benchmark performance, or physical execution."
            ),
        }
        final_status = (
            "completed" if vla_record["bounded_outcome_claimed"] else "failed"
        )
        recovery_artifacts = (
            {"missionos_vla_recovery_state": build_vla_recovery_state("not_required")}
            if final_status == "completed"
            else _vla_failure_recovery_artifacts(
                task_store=task_store,
                task_id=task_id,
                proposal=proposal,
                approval=approval,
                failure_evidence=vla_record,
            )
        )
        task_store.update(
            task_id,
            status=final_status,
            replace_artifacts={
                "missionos_vla_mission_run_record": vla_record,
                **recovery_artifacts,
            },
        )
        return vla_record
    except Exception as exc:
        failure_evidence = {
            "failure_type": type(exc).__name__,
            "raw_error_included": False,
            "mission_completion_claimed": False,
            "physical_execution_invoked": False,
        }
        task_store.update(
            task_id,
            status="failed",
            error=type(exc).__name__,
            artifacts={
                "physical_ai_execution_failure": failure_evidence,
                **_vla_failure_recovery_artifacts(
                    task_store=task_store,
                    task_id=task_id,
                    proposal=proposal,
                    approval=approval,
                    failure_evidence=failure_evidence,
                ),
            },
        )
        raise


def start_physical_ai_chat_execution(
    *,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any],
    session_id: str,
    task_store: TaskStore | None = None,
    repair_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the task and run it in a background worker."""

    execution_mode = physical_ai_chat_execution_mode()
    if execution_mode not in {"fixture", "live"}:
        raise RuntimeError(f"physical_ai_chat_execution_{execution_mode}")
    store = task_store or get_task_store()
    task = store.create(
        kind=(
            "parent_mission_execution"
            if proposal.get("mission_kind") == THREE_STAGE_MISSION_KIND
            else "vla_mission_execution"
        ),
        title=(
            "PX4 → Nav2 → GR00T/LIBERO governed mission"
            if proposal.get("mission_kind") == THREE_STAGE_MISSION_KIND
            else "GR00T N1.7 / LIBERO Panda governed mission"
        ),
        status="running",
        owner_session_id=session_id or None,
        run_id=str(proposal.get("parent_run_identity") or ""),
        # This is a Mission Contract approval artifact, not a ToolPolicy
        # approval id. Putting it in approval_dependencies would make the task
        # timeline query the unrelated tool-approval registry.
        approval_dependencies=[],
        artifacts={
            **_task_artifacts(proposal=proposal, approval=approval),
            **(
                {"missionos_vla_post_episode_repair_lineage": dict(repair_lineage)}
                if isinstance(repair_lineage, Mapping)
                else {}
            ),
            **(
                {
                    "missionos_vla_recovery_state": build_vla_recovery_state(
                        "monitoring"
                    )
                }
                if proposal.get("mission_kind") == VLA_ONLY_MISSION_KIND
                else {}
            ),
        },
        metadata={
            "execution_mode": execution_mode,
            "started_from": "missionos_chat",
            "started_at": _utc_now(),
            **(
                {
                    "vla_post_episode_repair_attempt_index": int(
                        repair_lineage.get("attempt_index", 0)
                    ),
                    "vla_post_episode_repair_source_task_id": str(
                        repair_lineage.get("source_task_id") or ""
                    ),
                }
                if isinstance(repair_lineage, Mapping)
                else {}
            ),
        },
    )
    task_id = str(task["task_id"])

    def worker() -> None:
        try:
            run_physical_ai_chat_execution(
                proposal=proposal,
                approval=approval,
                execution_mode=execution_mode,
                task_store=store,
                task_id=task_id,
            )
        except Exception:
            return

    threading.Thread(
        target=worker,
        name=f"missionos-physical-ai-{task_id}",
        daemon=True,
    ).start()
    return {
        "task_id": task_id,
        "task_status": "running",
        "execution_mode": execution_mode,
        "mission_completion_claimed": False,
        "physical_execution_invoked": False,
    }


__all__ = [
    "PHYSICAL_AI_CHAT_EXECUTION_MODE_ENV",
    "PHYSICAL_AI_CHAT_EXECUTION_OPT_IN_ENV",
    "PHYSICAL_AI_CHAT_FIXTURE_OPT_IN_ENV",
    "PHYSICAL_AI_LIBERO_STAGE_COMMAND_ENV",
    "build_vla_recovery_state",
    "physical_ai_chat_execution_mode",
    "run_physical_ai_chat_execution",
    "start_physical_ai_chat_execution",
]
