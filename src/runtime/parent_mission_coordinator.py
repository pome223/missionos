"""Thin coordinator for an approved, ordered parent mission.

The coordinator owns sequencing only. Stage runners keep all executor-specific
logic, and child predicate packages keep all outcome logic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from missionos_core import (
    FrozenParentMissionContract,
    ParentMissionApprovalBinding,
    ParentMissionStageResult,
    bind_parent_mission_stage_result,
    canonical_sha256,
    evaluate_parent_mission_transition_authority,
    validate_parent_mission_approval_binding,
)


PARENT_MISSION_RUN_RECORD_SCHEMA_VERSION = (
    "missionos_parent_mission_run_record.v1"
)
PARENT_MISSION_COORDINATOR_VERSION = "1"
_PARENT_MISSION_COORDINATOR_MATERIAL = {
    "version": PARENT_MISSION_COORDINATOR_VERSION,
    "run_record_schema_version": PARENT_MISSION_RUN_RECORD_SCHEMA_VERSION,
    "sequencing": "approved_stage_order",
    "transition_authority_source": "preexisting_parent_mission_approval",
    "previous_stage_predicate_role": "prerequisite_only",
    "stop_condition": "first_blocked_or_unsatisfied_stage",
    "stage_result_binding": "contract_stage_and_predicate_package",
    "parent_mission_completion_claimed": False,
    "identity_continuity_claimed": False,
    "shared_world_claimed": False,
    "dispatch_authority_created": False,
    "physical_execution_invoked": False,
}
PARENT_MISSION_COORDINATOR_MATERIAL_SHA256 = canonical_sha256(
    _PARENT_MISSION_COORDINATOR_MATERIAL
)

ParentMissionStageRunner = Callable[[], Mapping[str, Any] | Any]
ParentMissionProgressCallback = Callable[[Mapping[str, Any]], None]


def _progress_snapshot(
    *,
    contract: FrozenParentMissionContract,
    approval: ParentMissionApprovalBinding | None,
    records: list[dict[str, Any]],
    blocking_reasons: list[str],
    current_stage_ref: str | None,
) -> dict[str, Any]:
    """Build a non-promoting coordinator snapshot for operator monitoring."""

    satisfied_count = sum(
        1
        for record in records
        if isinstance(record.get("stage_result"), Mapping)
        and record["stage_result"].get("predicate_satisfied") is True
    )
    invoked_refs = {
        str(record.get("stage_ref") or "")
        for record in records
        if record.get("runner_invoked") is True
    }
    return {
        "schema_version": PARENT_MISSION_RUN_RECORD_SCHEMA_VERSION,
        "parent_mission_id": contract.parent_mission_id,
        "parent_mission_sha256": contract.parent_mission_sha256,
        "approval_binding_sha256": (
            approval.approval_binding_sha256
            if isinstance(approval, ParentMissionApprovalBinding)
            else ""
        ),
        "current_stage_ref": current_stage_ref,
        "stage_count": len(contract.stages),
        "stage_records": [dict(record) for record in records],
        "stages_satisfied": satisfied_count,
        "unreached_stage_refs": [
            stage.stage_ref
            for stage in contract.stages
            if stage.stage_ref not in invoked_refs
        ],
        "coordinator_status": (
            "running"
            if current_stage_ref
            else "stages_satisfied"
            if satisfied_count == len(contract.stages) and not blocking_reasons
            else "blocked"
        ),
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
        "mission_completion_claimed": False,
        "mission_completion_status": "unverified",
        "identity_continuity_claimed": False,
        "shared_world_claimed": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "operational_closure_created": False,
        "physical_execution_invoked": False,
        "claim_boundary": (
            "Satisfied child predicates remain stage-scoped. Their conjunction "
            "does not establish parent mission completion, physical identity, "
            "or a shared world. Every stage authority comes from the exact "
            "pre-existing parent mission approval."
        ),
    }


def _evaluation_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    return None


def run_parent_mission_coordinator(
    *,
    contract: FrozenParentMissionContract,
    approval: ParentMissionApprovalBinding | None,
    stage_runners: Mapping[str, ParentMissionStageRunner],
    progress_callback: ParentMissionProgressCallback | None = None,
) -> dict[str, Any]:
    """Run exact approved stages until a boundary blocks.

    A child predicate result is a prerequisite for the next stage. It is never
    the authority source and never becomes a parent mission completion claim.
    """

    expected_refs = tuple(stage.stage_ref for stage in contract.stages)
    provided_refs = tuple(stage_runners.keys())
    configuration_reasons = list(
        validate_parent_mission_approval_binding(
            contract=contract,
            approval=approval,
        )
    )
    missing_refs = sorted(set(expected_refs) - set(provided_refs))
    unexpected_refs = sorted(set(provided_refs) - set(expected_refs))
    configuration_reasons.extend(
        f"parent_mission_coordinator_runner_missing:{stage_ref}"
        for stage_ref in missing_refs
    )
    configuration_reasons.extend(
        f"parent_mission_coordinator_runner_unexpected:{stage_ref}"
        for stage_ref in unexpected_refs
    )

    records: list[dict[str, Any]] = []
    blocking_reasons = list(dict.fromkeys(configuration_reasons))
    previous_result: ParentMissionStageResult | None = None
    previous_evaluation: dict[str, Any] | None = None
    if not blocking_reasons:
        for stage in contract.stages:
            transition = evaluate_parent_mission_transition_authority(
                contract=contract,
                approval=approval,
                target_stage_index=stage.stage_index,
                target_stage_ref=stage.stage_ref,
                previous_stage_result=previous_result,
                previous_predicate_evaluation=previous_evaluation,
            )
            stage_record: dict[str, Any] = {
                "stage_index": stage.stage_index,
                "stage_ref": stage.stage_ref,
                "executor_ref": stage.executor_ref,
                "stage_binding_sha256": stage.stage_binding_sha256,
                "transition_authority": transition.to_dict(),
                "runner_invoked": False,
                "predicate_evaluation": None,
                "stage_result": None,
            }
            if not transition.dispatch_authority_present:
                blocking_reasons.extend(transition.blocking_reasons)
                records.append(stage_record)
                break

            if progress_callback is not None:
                progress_callback(
                    _progress_snapshot(
                        contract=contract,
                        approval=approval,
                        records=[*records, stage_record],
                        blocking_reasons=blocking_reasons,
                        current_stage_ref=stage.stage_ref,
                    )
                )

            try:
                evaluation = _evaluation_mapping(
                    stage_runners[stage.stage_ref]()
                )
            except Exception as exc:  # pragma: no cover - exact exception varies
                evaluation = None
                blocking_reasons.append(
                    "parent_mission_coordinator_runner_failed:"
                    f"{stage.stage_ref}:{type(exc).__name__}"
                )
            stage_record["runner_invoked"] = True
            if evaluation is None:
                blocking_reasons.append(
                    "parent_mission_coordinator_evaluation_invalid:"
                    f"{stage.stage_ref}"
                )
                records.append(stage_record)
                break

            result = bind_parent_mission_stage_result(
                contract=contract,
                stage_index=stage.stage_index,
                predicate_evaluation=evaluation,
            )
            stage_record["predicate_evaluation"] = evaluation
            stage_record["stage_result"] = {
                **result.to_material(),
                "stage_result_sha256": result.stage_result_sha256,
            }
            records.append(stage_record)
            if not result.predicate_satisfied:
                blocking_reasons.extend(result.reasons)
                if not result.reasons:
                    blocking_reasons.append(
                        "parent_mission_coordinator_stage_not_satisfied:"
                        f"{stage.stage_ref}"
                    )
                break
            previous_result = result
            previous_evaluation = evaluation
    final = _progress_snapshot(
        contract=contract,
        approval=approval,
        records=records,
        blocking_reasons=blocking_reasons,
        current_stage_ref=None,
    )
    if progress_callback is not None:
        progress_callback(final)
    return final


__all__ = [
    "PARENT_MISSION_COORDINATOR_MATERIAL_SHA256",
    "PARENT_MISSION_COORDINATOR_VERSION",
    "PARENT_MISSION_RUN_RECORD_SCHEMA_VERSION",
    "ParentMissionProgressCallback",
    "ParentMissionStageRunner",
    "run_parent_mission_coordinator",
]
