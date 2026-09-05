"""Validate the bounded GR00T recovery-training compute preflight.

This is a planning boundary. A valid record cannot authorize provisioning,
training, inference, dispatch, or physical execution.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path
import re
from typing import Any

from src.runtime.libero_recovery_training_phase0 import canonical_sha256


SCHEMA_VERSION = "missionos.libero_recovery_training_preflight.v1"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def validate_training_preflight(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("preflight_schema_version_mismatch")
    material = {key: value for key, value in record.items() if key != "result_sha256"}
    if record.get("result_sha256") != canonical_sha256(material):
        errors.append("preflight_result_digest_mismatch")

    checkpoint = _mapping(record.get("checkpoint"))
    if checkpoint.get("model_id") != "nvidia/GR00T-N1.7-3B":
        errors.append("preflight_checkpoint_model_mismatch")
    if not _SHA40.fullmatch(str(checkpoint.get("revision", ""))):
        errors.append("preflight_checkpoint_revision_required")
    if checkpoint.get("license_scope") != "research_and_evaluation_only":
        errors.append("preflight_research_only_license_required")
    if checkpoint.get("commercial_use_authorized") is not False:
        errors.append("preflight_commercial_use_must_remain_false")
    if checkpoint.get("derivative_weights_publication_authorized") is not False:
        errors.append("preflight_derivative_publication_must_remain_false")

    backbone = _mapping(record.get("dependent_backbone"))
    if backbone.get("model_id") != "nvidia/Cosmos-Reason2-2B":
        errors.append("preflight_backbone_model_mismatch")
    if backbone.get("gating") != "auto" or backbone.get("access_verified") is not False:
        errors.append("preflight_backbone_access_must_remain_unverified")

    dataset = _mapping(record.get("dataset"))
    if dataset.get("admission_status") != "admitted":
        errors.append("preflight_dataset_admission_required")
    if dataset.get("training_examples_admitted") != 4:
        errors.append("preflight_dataset_count_mismatch")
    if not _SHA64.fullmatch(str(dataset.get("manifest_file_sha256", ""))):
        errors.append("preflight_dataset_manifest_digest_required")

    hardware = _mapping(record.get("hardware"))
    minimum_vram = hardware.get("official_minimum_vram_gb")
    if not isinstance(minimum_vram, int) or minimum_vram < 40:
        errors.append("preflight_official_vram_minimum_invalid")
    if hardware.get("l4_vram_gb") != 24 or hardware.get("l4_training_eligible") is not False:
        errors.append("preflight_l4_must_be_rejected")
    quota = _mapping(hardware.get("observed_project_quota"))
    if quota.get("nvidia_a100_gpus_limit") != 0:
        errors.append("preflight_observed_a100_quota_mismatch")

    estimate = _mapping(record.get("runtime_estimate"))
    if estimate.get("measured_pilot_gpu_hours") is not None:
        errors.append("preflight_unmeasured_gpu_hours_must_be_null")
    if estimate.get("reference_runtime_extrapolated_to_this_dataset") is not False:
        errors.append("preflight_reference_runtime_must_not_be_extrapolated")

    cost = _mapping(record.get("cost_control"))
    if cost.get("currency") != "JPY":
        errors.append("preflight_cost_currency_must_be_jpy")
    cap = cost.get("proposed_hard_cap_jpy")
    guard = cost.get("prelaunch_estimate_ceiling_jpy")
    if (
        not isinstance(cap, int)
        or isinstance(cap, bool)
        or cap <= 0
        or not isinstance(guard, int)
        or isinstance(guard, bool)
        or guard <= 0
        or guard >= cap
    ):
        errors.append("preflight_cost_cap_invalid")
    if cost.get("cost_cap_reviewed") is not False:
        errors.append("preflight_unreviewed_cost_cap_must_remain_false")
    if cost.get("maximum_runtime_seconds") != 3600:
        errors.append("preflight_runtime_cap_mismatch")
    if cost.get("maximum_gpu_count") != 1:
        errors.append("preflight_gpu_count_cap_mismatch")
    if cost.get("termination_action") != "DELETE":
        errors.append("preflight_delete_termination_required")
    if cost.get("boot_disk_auto_delete") is not True:
        errors.append("preflight_boot_disk_auto_delete_required")
    if cost.get("billing_budget_is_hard_stop") is not False:
        errors.append("preflight_budget_must_not_claim_hard_stop")

    decision = _mapping(record.get("decision"))
    if decision.get("status") != "NO_GO":
        errors.append("preflight_decision_must_be_no_go")
    if decision.get("paid_training_authorized") is not False:
        errors.append("preflight_paid_training_must_be_false")
    if decision.get("gpu_provision_authorized") is not False:
        errors.append("preflight_gpu_provision_must_be_false")
    blockers = decision.get("blocking_reasons")
    required_blockers = {
        "dependent_backbone_access_not_verified",
        "research_only_model_license_not_reviewed",
        "minimum_40gb_gpu_quota_unavailable",
        "pilot_gpu_hours_not_measured",
        "hard_cost_cap_not_reviewed",
    }
    if not isinstance(blockers, list) or not required_blockers.issubset(blockers):
        errors.append("preflight_blocking_reasons_incomplete")

    boundary = _mapping(record.get("claim_boundary"))
    for field in (
        "gpu_provisioned",
        "training_invoked",
        "model_inference_invoked",
        "dispatch_request_sent",
        "physical_execution_invoked",
        "repair_capability_established",
    ):
        if boundary.get(field) is not False:
            errors.append(f"preflight_claim_boundary_invalid:{field}")
    return errors


def validate_dataset_manifest_file(
    record: Mapping[str, Any], repository_root: Path
) -> list[str]:
    dataset = _mapping(record.get("dataset"))
    reference = dataset.get("manifest_ref")
    expected = dataset.get("manifest_file_sha256")
    if not isinstance(reference, str) or not _SHA64.fullmatch(str(expected or "")):
        return ["preflight_dataset_manifest_reference_invalid"]
    root = repository_root.resolve()
    candidate = (root / reference).resolve()
    if not candidate.is_relative_to(root):
        return ["preflight_dataset_manifest_outside_repository"]
    try:
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return ["preflight_dataset_manifest_unreadable"]
    return [] if actual == expected else ["preflight_dataset_manifest_file_digest_mismatch"]


def training_preflight_summary(
    record: Mapping[str, Any], *, repository_root: Path | None = None
) -> dict[str, Any]:
    errors = validate_training_preflight(record)
    if repository_root is not None:
        errors.extend(validate_dataset_manifest_file(record, repository_root))
    decision = _mapping(record.get("decision"))
    return {
        "status": "valid" if not errors else "invalid",
        "decision": decision.get("status"),
        "paid_training_authorized": False,
        "gpu_provision_authorized": False,
        "errors": errors,
    }
