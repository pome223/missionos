"""Project the published GR00T Repair cohort into the five-axis contract.

This module performs no model inference.  It preserves missing observations as
``not_observed`` and binds every observed judgment to the published cohort
records that already support it.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from missionos_core import (
    RepairAxisObservation,
    RepairAxisStatus,
    RepairDiagnosticAxis,
    RepairDiagnosticContext,
    RepairDiagnosticValidationStatus,
    RepairEvidenceBasis,
    canonical_sha256,
    evaluate_repair_diagnostics,
)


PROJECTION_SCHEMA_VERSION = "missionos_groot_repair_diagnostic_projection.v1"
EXPECTED_COHORT_SCHEMA = "missionos_groot_lerobot_repair_loop_cohort.v1"
EXPECTED_PUBLICATION_SCHEMA = (
    "missionos.groot_lerobot_native_single_attempt_cohort_publication.v1"
)
EXECUTOR_REF = "huggingface:nvidia/GR00T-N1.7-3B"
REQUIRED_HOLD_STEPS = 20


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"groot_repair_projection_object_required:{path}")
    return value


def _criterion(material: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    retained = deepcopy(dict(material))
    return f"sha256:{canonical_sha256(retained)}", retained


def _not_observed(
    axis: RepairDiagnosticAxis,
    *,
    scope_ref: str,
    criterion: Mapping[str, Any],
    reason: str,
) -> RepairAxisObservation:
    criterion_ref, material = _criterion(criterion)
    return RepairAxisObservation(
        axis=axis,
        status=RepairAxisStatus.NOT_OBSERVED,
        evidence_basis=RepairEvidenceBasis.NOT_OBSERVED,
        criterion_ref=criterion_ref,
        observation_scope_ref=scope_ref,
        measurements={"criterion": material, "not_observed_reason": reason},
    )


def _validate_inputs(
    cohort: Mapping[str, Any],
    publication: Mapping[str, Any],
    *,
    cohort_sha256: str,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if cohort.get("schema_version") != EXPECTED_COHORT_SCHEMA:
        raise ValueError("groot_repair_projection_cohort_schema_mismatch")
    if publication.get("schema_version") != EXPECTED_PUBLICATION_SCHEMA:
        raise ValueError("groot_repair_projection_publication_schema_mismatch")

    cohort_without_digest = {key: value for key, value in cohort.items() if key != "result_sha256"}
    result_sha256 = cohort.get("result_sha256")
    if result_sha256 != canonical_sha256(cohort_without_digest):
        raise ValueError("groot_repair_projection_cohort_result_digest_mismatch")

    published_result = publication.get("cohort_result")
    if not isinstance(published_result, Mapping):
        raise TypeError("groot_repair_projection_publication_result_required")
    if published_result.get("source_record_sha256") != cohort_sha256:
        raise ValueError("groot_repair_projection_source_file_digest_mismatch")
    if published_result.get("result_sha256") != result_sha256:
        raise ValueError("groot_repair_projection_published_result_digest_mismatch")

    loops = cohort.get("loops")
    contexts = publication.get("native_loop_context")
    if not isinstance(loops, list) or not isinstance(contexts, list):
        raise TypeError("groot_repair_projection_loop_lists_required")
    if len(loops) != len(contexts) or len(loops) != cohort.get("completed_loop_count"):
        raise ValueError("groot_repair_projection_loop_count_mismatch")
    if publication.get("cohort_result", {}).get("completed_loop_count") != len(loops):
        raise ValueError("groot_repair_projection_published_loop_count_mismatch")

    for index, (loop, context) in enumerate(zip(loops, contexts, strict=True)):
        if not isinstance(loop, Mapping) or not isinstance(context, Mapping):
            raise TypeError("groot_repair_projection_loop_object_required")
        if loop.get("cohort_index") != index or context.get("cohort_index") != index:
            raise ValueError("groot_repair_projection_loop_index_mismatch")
        source = context.get("source_goal_predicate_vector")
        terminal = context.get("terminal_goal_predicate_vector")
        target = context.get("target_predicate_index")
        preserve = context.get("preserve_predicate_indices")
        if (
            not isinstance(source, list)
            or not source
            or not all(isinstance(item, bool) for item in source)
            or not isinstance(terminal, list)
            or len(terminal) != len(source)
            or not all(isinstance(item, bool) for item in terminal)
            or isinstance(target, bool)
            or not isinstance(target, int)
            or target < 0
            or target >= len(source)
            or not isinstance(preserve, list)
            or not preserve
            or not all(
                not isinstance(item, bool) and isinstance(item, int) and 0 <= item < len(source)
                for item in preserve
            )
        ):
            raise ValueError("groot_repair_projection_predicate_context_invalid")
        if target in preserve or sorted([target, *preserve]) != list(range(len(source))):
            raise ValueError("groot_repair_projection_predicate_partition_invalid")
        if source[target] is not False:
            raise ValueError("groot_repair_projection_source_target_not_failed")
        if any(source[item] is not True for item in preserve):
            raise ValueError("groot_repair_projection_source_preservation_not_satisfied")
        target_recovered = terminal[target] is True
        preservation_satisfied = all(terminal[item] is True for item in preserve)
        if target_recovered is not bool(loop.get("semantic_repair_established")):
            raise ValueError("groot_repair_projection_recovery_result_mismatch")
        if preservation_satisfied is bool(loop.get("preservation_violation_observed")):
            raise ValueError("groot_repair_projection_preservation_result_mismatch")
    return loops, contexts


def project_groot_repair_diagnostics(
    cohort_path: Path,
    publication_path: Path,
) -> dict[str, Any]:
    """Return deterministic five-axis reports for the published five-loop cohort."""

    cohort = _load_object(cohort_path)
    publication = _load_object(publication_path)
    cohort_sha256 = _file_sha256(cohort_path)
    publication_sha256 = _file_sha256(publication_path)
    loops, contexts = _validate_inputs(cohort, publication, cohort_sha256=cohort_sha256)

    reports: list[dict[str, Any]] = []
    for index, (loop, native) in enumerate(zip(loops, contexts, strict=True)):
        scope_ref = f"sha256:{publication_sha256}#/native_loop_context/{index}"
        publication_ref = f"sha256:{publication_sha256}#/native_loop_context/{index}"
        cohort_ref = f"sha256:{cohort_sha256}#/loops/{index}"
        source = list(native["source_goal_predicate_vector"])
        terminal = list(native["terminal_goal_predicate_vector"])
        target = int(native["target_predicate_index"])
        preserve = list(native["preserve_predicate_indices"])

        predicate_criterion = {
            "rule": "failed_target_predicate_must_transition_to_true",
            "target_predicate_index": target,
        }
        predicate_ref, predicate_material = _criterion(predicate_criterion)
        preservation_criterion = {
            "rule": "all_contract_bound_preserve_predicates_remain_true",
            "preserve_predicate_indices": preserve,
        }
        preservation_ref, preservation_material = _criterion(preservation_criterion)

        observations = (
            _not_observed(
                RepairDiagnosticAxis.ACTION_ACTIVITY,
                scope_ref=scope_ref,
                criterion={
                    "rule": "meaningful_executor_action_or_effect_must_be_observed",
                },
                reason="published_cohort_does_not_retain_action_magnitude_or_effect_trace",
            ),
            _not_observed(
                RepairDiagnosticAxis.CORRECTIVE_ALIGNMENT,
                scope_ref=scope_ref,
                criterion={
                    "rule": "action_or_effect_must_align_with_preregistered_corrective_reference",
                },
                reason="published_cohort_does_not_retain_corrective_reference_or_alignment_trace",
            ),
            RepairAxisObservation(
                axis=RepairDiagnosticAxis.PREDICATE_RECOVERY,
                status=(
                    RepairAxisStatus.SATISFIED
                    if terminal[target]
                    else RepairAxisStatus.NOT_SATISFIED
                ),
                evidence_basis=RepairEvidenceBasis.SIMULATOR_OBSERVATION,
                criterion_ref=predicate_ref,
                observation_scope_ref=scope_ref,
                evidence_refs=(publication_ref, cohort_ref),
                measurements={
                    "criterion": predicate_material,
                    "source_goal_predicate_vector": source,
                    "terminal_goal_predicate_vector": terminal,
                    "target_predicate_index": target,
                    "source_result_sha256": native["source_result_sha256"],
                },
            ),
            RepairAxisObservation(
                axis=RepairDiagnosticAxis.PRESERVATION,
                status=(
                    RepairAxisStatus.SATISFIED
                    if not loop["preservation_violation_observed"]
                    else RepairAxisStatus.NOT_SATISFIED
                ),
                evidence_basis=RepairEvidenceBasis.SIMULATOR_OBSERVATION,
                criterion_ref=preservation_ref,
                observation_scope_ref=scope_ref,
                evidence_refs=(publication_ref, cohort_ref),
                measurements={
                    "criterion": preservation_material,
                    "source_goal_predicate_vector": source,
                    "terminal_goal_predicate_vector": terminal,
                    "preservation_violation_observed": loop[
                        "preservation_violation_observed"
                    ],
                },
            ),
            _not_observed(
                RepairDiagnosticAxis.STABLE_HOLD,
                scope_ref=scope_ref,
                criterion={
                    "rule": "full_predicate_conjunction_must_hold_for_contiguous_steps",
                    "required_hold_steps": REQUIRED_HOLD_STEPS,
                },
                reason="predicate_conjunction_not_reached_so_stable_hold_was_not_admitted",
            ),
        )
        assessment = evaluate_repair_diagnostics(
            observations,
            context=RepairDiagnosticContext(
                report_id=f"repair-diagnostic:groot-n17-native-cohort:{index}",
                executor_ref=EXECUTOR_REF,
                task_ref="publication-bound:libero-native-repair-cohort",
                fixture_ref=(
                    f"source-result:{native['source_result_sha256']}:"
                    f"init-state:{native['episode_init_state_index']}"
                ),
                evaluation_scope="simulator_live_same_world_publication_projection",
            ),
        )
        if assessment.validation_status is not RepairDiagnosticValidationStatus.VERIFIED:
            raise ValueError(
                "groot_repair_projection_core_validation_failed:"
                + ",".join(assessment.reasons)
            )
        reports.append(assessment.to_dict())

    body = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "projection_kind": "existing_public_evidence_only",
        "fresh_inference_performed": False,
        "paid_compute_used": False,
        "source_bindings": {
            "cohort_record": cohort_path.name,
            "cohort_record_sha256": cohort_sha256,
            "cohort_result_sha256": cohort["result_sha256"],
            "publication_record": publication_path.name,
            "publication_record_sha256": publication_sha256,
        },
        "report_count": len(reports),
        "reports": reports,
        "claim_boundary": {
            "missing_axes_backfilled": False,
            "new_approval_created": False,
            "new_dispatch_authority_created": False,
            "new_execution_claimed": False,
            "executor_repair_capability_established": False,
            "physical_execution_invoked": False,
        },
    }
    return {**body, "result_sha256": canonical_sha256(body)}


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "project_groot_repair_diagnostics",
]
