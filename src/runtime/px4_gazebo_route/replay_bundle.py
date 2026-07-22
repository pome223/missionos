from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from src.runtime.px4_gazebo_route.recovery_intent_compiler import (
    recovery_artifact_hash_matches,
)


BUNDLE_SCHEMA_VERSION = "missionos_anonymized_recovery_replay_bundle.v1"
VERDICT_SCHEMA_VERSION = "missionos_anonymized_recovery_replay_verdict.v1"

_PUBLIC_RUN_REF_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,95}$")
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "artifact_dir",
        "artifact_path",
        "api_key",
        "authorization",
        "credential",
        "database_path",
        "db_path",
        "flight_path_trace_path",
        "owner_session_id",
        "owner_user_id",
        "prompt",
        "prompt_text",
        "response_text",
        "secret",
        "task_id",
        "token",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _public_ref(value: Any, *, prefix: str) -> str:
    text = str(value or "").strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:12]}"


def _finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    if parsed.is_integer() and isinstance(value, int):
        return int(parsed)
    return round(parsed, 6)


def _selected(source: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    return {name: source.get(name) for name in names if name in source}


def _receipt_hash_matches(receipt: Mapping[str, Any]) -> bool:
    expected = str(receipt.get("published_dispatch_receipt_sha256") or "")
    unhashed = {
        key: value
        for key, value in receipt.items()
        if key
        not in {
            "published_dispatch_receipt_id",
            "published_dispatch_receipt_sha256",
        }
    }
    return bool(expected and expected == _canonical_sha256(unhashed))


def _bounded_parameters_preserve_compilation(
    observed: Any,
    compiled: Any,
) -> bool:
    observed_parameters = _mapping(observed)
    compiled_parameters = _mapping(compiled)
    if any(
        observed_parameters.get(key) != value
        for key, value in compiled_parameters.items()
    ):
        return False
    extras = set(observed_parameters) - set(compiled_parameters)
    return not extras or (
        extras == {"obstacle_avoidance_required"}
        and observed_parameters.get("obstacle_avoidance_required") is True
    )


def _sanitize_intent(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    return _selected(
        source,
        (
            "schema_version",
            "intent_status",
            "strategy",
            "selected_action",
            "intent_constraints",
            "requested_parameters",
            "rationale",
            "observed_at",
            "decision_signature",
            "blocking_reasons",
            "requires_human_approval",
            "approval_created",
            "dispatch_authority_created",
            "physical_execution_invoked",
            "progress_counted",
            "recovery_intent_sha256",
            "recovery_intent_id",
        ),
    )


def _sanitize_compilation(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    return _selected(
        source,
        (
            "schema_version",
            "compilation_status",
            "source_intent_id",
            "source_intent_sha256",
            "requested_action",
            "requested_strategy",
            "requested_parameters",
            "intent_constraints",
            "compiled_action",
            "compiled_parameters",
            "meaning_preserved",
            "candidate_basis",
            "candidate_source_refs",
            "policy_ref",
            "policy_snapshot",
            "blocking_reasons",
            "approval_created",
            "dispatch_authority_created",
            "physical_execution_invoked",
            "progress_counted",
            "recovery_compilation_sha256",
            "recovery_compilation_id",
        ),
    )


def _sanitize_reachability(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    return _selected(
        source,
        (
            "schema_version",
            "verification_status",
            "source_compilation_id",
            "source_compilation_sha256",
            "action",
            "horizontal_distance_m",
            "vertical_distance_m",
            "max_horizontal_speed_mps",
            "conservative_horizontal_speed_mps",
            "max_vertical_speed_mps",
            "wind_speed_mps",
            "wind_uncertainty_mps",
            "estimated_duration_s",
            "upper_bound_duration_s",
            "available_duration_s",
            "reachability_verified",
            "blocking_reasons",
            "dispatch_authority_created",
            "physical_execution_invoked",
            "progress_counted",
            "recovery_reachability_sha256",
            "recovery_reachability_id",
        ),
    )


def _sanitize_outcome(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    return _selected(
        source,
        (
            "schema_version",
            "verification_status",
            "action",
            "dispatch_authority_observed",
            "command_ack_observed",
            "executor_effect_observed",
            "target_reached",
            "resume_status",
            "resume_safety_verification",
            "ack_is_execution_effect",
            "recovery_success_verified",
            "blocking_reasons",
            "delivery_completion_claimed",
            "physical_execution_invoked",
            "progress_counted",
            "recovery_outcome_verification_sha256",
            "recovery_outcome_verification_id",
        ),
    )


def _sanitize_proposal(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    proposal = _selected(
        source,
        (
            "schema_version",
            "proposal_id",
            "proposal_status",
            "observed_at",
            "valid_until",
            "sample_index",
            "decision_signature_version",
            "recovery_decision_signature",
            "proposal_origin",
            "proposal_origin_sha256",
            "proposal_source",
            "hosted_model_invoked_for_proposal",
            "hosted_model_judgment_used_for_proposal",
            "claimed_by_approval_ref",
            "dispatch_status",
            "dispatch_authority_created",
            "physical_execution_invoked",
            "progress_counted",
        ),
    )
    proposal["recovery_intent"] = _sanitize_intent(source.get("recovery_intent"))
    proposal["intent_compilation"] = _sanitize_compilation(
        source.get("intent_compilation")
    )
    proposal["reachability_verification"] = _sanitize_reachability(
        source.get("reachability_verification")
    )
    return proposal


def _approval_from_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    approval = _mapping(receipt.get("maneuver_approval"))
    if not approval:
        approval = _mapping(receipt.get("emergency_command_approval"))
    return _selected(
        approval,
        (
            "schema_version",
            "approval_id",
            "operator_approval_performed",
            "approved_recovery_action",
            "approved_recovery_actions",
            "approved_parameters",
            "operator_surface",
            "explicit_recovery_dispatch_approval",
            "approval_free_recovery_dispatch_allowed",
            "delivery_completion_claimed",
            "physical_execution_invoked",
            "hardware_target_allowed",
            "approved_at",
        ),
    )


def _sanitize_receipt(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    revalidation = _mapping(source.get("proposal_revalidation"))
    public_payload = {
        **_selected(
            source,
            (
                "schema_version",
                "dispatch_status",
                "recovery_action",
                "recovery_parameters",
                "operator_approved",
                "explicit_recovery_dispatch_approval",
                "active_runner_request_queued",
                "blocked_reasons",
                "dispatch_authority_created",
                "delivery_completion_claimed",
                "progress_counted",
                "physical_execution_invoked",
                "hardware_target_allowed",
                "observed_at",
            ),
        ),
        "source_dispatch_receipt_ref": _public_ref(
            source.get("dispatch_receipt_id") or source.get("observed_at"),
            prefix="source_receipt",
        ),
        "source_dispatch_receipt_sha256_attested": str(
            source.get("dispatch_receipt_sha256") or ""
        ),
        "approval": _approval_from_receipt(source),
        "proposal_revalidation": _selected(
            revalidation,
            (
                "schema_version",
                "validation_status",
                "proposal_id",
                "proposal_observed_at",
                "valid_until",
                "proposal_origin_sha256",
                "action_matches",
                "parameters_match",
                "intent_compiler_contract_required",
                "recovery_intent_id",
                "recovery_compilation_id",
                "stored_recovery_reachability_id",
                "telemetry_fresh",
                "dispatch_reachability_verification",
                "reasons",
                "dispatch_authority_created",
                "physical_execution_invoked",
                "progress_counted",
            ),
        ),
    }
    public_digest = _canonical_sha256(public_payload)
    return {
        **public_payload,
        "published_dispatch_receipt_sha256": public_digest,
        "published_dispatch_receipt_id": f"published_dispatch_receipt_{public_digest[:12]}",
    }


def _sanitize_attempt(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    position = _mapping(source.get("position"))
    return {
        **_selected(
            source,
            (
                "schema_version",
                "attempt_id",
                "source_proposal_id",
                "proposal_origin_sha256",
                "observed_at",
                "sample_index",
                "attempt_status",
                "recovery_action",
                "recovery_parameters",
                "command_ack_observed",
                "assist_attempted",
                "assist_status",
                "target_reached",
                "target_distance_m",
                "resume_status",
                "resume_auto_attempted",
                "resume_safety_verification",
                "outcome_verification_id",
                "outcome_verification_sha256",
                "dispatch_authority_created",
                "simulator_execution_observed",
                "delivery_completion_claimed",
                "physical_execution_invoked",
                "progress_counted",
            ),
        ),
        "position": _selected(
            position,
            (
                "local_x_m",
                "local_y_m",
                "local_z_m",
                "altitude_above_home_m",
                "distance_to_home_m",
            ),
        ),
        "outcome_verification": _sanitize_outcome(
            source.get("outcome_verification")
        ),
    }


def _sanitize_telemetry_sample(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    result: dict[str, Any] = {}
    for name in (
        "sample_index",
        "phase",
        "relative_alt_m",
        "local_x_m",
        "local_y_m",
        "local_z_m",
        "horizontal_progress_m",
        "elapsed_s",
        "seq_reached",
        "mission_current_seq",
        "battery_remaining_percent",
        "battery_warning",
        "battery_status_observed",
        "heartbeat_observed",
    ):
        if name not in source:
            continue
        value_at_name = source.get(name)
        numeric = _finite_number(value_at_name)
        result[name] = numeric if numeric is not None else value_at_name
    return result


def _receipt_collection(artifacts: Mapping[str, Any]) -> list[dict[str, Any]]:
    receipts = [
        dict(item)
        for item in _mapping(
            artifacts.get("missionos_runtime_recovery_dispatch_receipts")
        ).values()
        if isinstance(item, Mapping)
    ]
    latest = artifacts.get("missionos_runtime_recovery_dispatch_receipt")
    if isinstance(latest, Mapping):
        latest_id = str(latest.get("dispatch_receipt_id") or "")
        if not any(str(item.get("dispatch_receipt_id") or "") == latest_id for item in receipts):
            receipts.append(dict(latest))
    return receipts


def _receipt_for_proposal(
    receipts: Sequence[Mapping[str, Any]],
    *,
    proposal_id: str,
) -> dict[str, Any]:
    for receipt in receipts:
        revalidation = _mapping(receipt.get("proposal_revalidation"))
        if str(revalidation.get("proposal_id") or "") == proposal_id:
            return dict(receipt)
    return {}


def build_anonymized_recovery_replay_bundle(
    task: Mapping[str, Any],
    *,
    public_run_ref: str,
    max_telemetry_samples: int = 240,
) -> dict[str, Any]:
    """Build a local-coordinate, authority-bounded replay publication artifact.

    The output intentionally excludes raw task ids, owners, database paths,
    artifact paths, WGS84 coordinates, prompts, model responses, and secrets.
    """

    normalized_run_ref = str(public_run_ref or "").strip().lower()
    if not _PUBLIC_RUN_REF_PATTERN.fullmatch(normalized_run_ref):
        raise ValueError(
            "public_run_ref must be 3-96 lowercase characters using a-z, 0-9, ., _, or -"
        )
    if normalized_run_ref.startswith("task_"):
        raise ValueError("public_run_ref must not expose a task id")

    artifacts = _mapping(task.get("artifacts"))
    proposals = _mapping(artifacts.get("missionos_runtime_recovery_proposals"))
    attempts = _mapping(artifacts.get("missionos_runtime_recovery_attempts"))
    receipts = _receipt_collection(artifacts)
    attempts_by_proposal = {
        str(attempt.get("source_proposal_id") or ""): dict(attempt)
        for attempt in attempts.values()
        if isinstance(attempt, Mapping)
        and str(attempt.get("source_proposal_id") or "")
    }

    epochs: list[dict[str, Any]] = []
    for proposal in sorted(
        (dict(item) for item in proposals.values() if isinstance(item, Mapping)),
        key=lambda item: str(item.get("observed_at") or ""),
    ):
        proposal_id = str(proposal.get("proposal_id") or "")
        attempt = attempts_by_proposal.get(proposal_id, {})
        if not attempt:
            continue
        receipt = _receipt_for_proposal(receipts, proposal_id=proposal_id)
        claimed_approval_ref = str(proposal.get("claimed_by_approval_ref") or "")
        approval_binding: dict[str, Any]
        if receipt:
            approval_binding = {
                "evidence_kind": "full_dispatch_receipt",
                "approval_ref": claimed_approval_ref,
                "receipt": _sanitize_receipt(receipt),
            }
        else:
            approval_binding = {
                "evidence_kind": "reference_only",
                "approval_ref": claimed_approval_ref,
                "limitation": "historical_dispatch_receipt_not_preserved_in_source_task",
            }
        epochs.append(
            {
                "epoch_index": len(epochs) + 1,
                "proposal": _sanitize_proposal(proposal),
                "approval_and_dispatch": approval_binding,
                "observation": _sanitize_attempt(attempt),
            }
        )

    replay = _mapping(artifacts.get("missionos_auto_mission_runtime_replay"))
    profile = [
        _sanitize_telemetry_sample(item)
        for item in _sequence(replay.get("flight_path_profile"))[:max_telemetry_samples]
        if isinstance(item, Mapping)
    ]
    monitor = _mapping(
        artifacts.get("missionos_auto_mission_runtime_monitor_summary")
    )
    waypoint_gate = _mapping(
        artifacts.get("missionos_auto_mission_waypoint_gate_summary")
    )
    dropoff_gate = _mapping(
        artifacts.get("missionos_auto_mission_dropoff_gate_summary")
    )
    delivery_gate = _mapping(
        artifacts.get("missionos_auto_mission_sitl_delivery_gate_summary")
    )
    payload_gate = _mapping(
        artifacts.get("missionos_auto_mission_payload_release_sim_gate_summary")
    )

    source_snapshot = {
        "kind": task.get("kind"),
        "status": task.get("status"),
        "artifacts": artifacts,
    }
    bundle_without_hash: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "public_run_ref": normalized_run_ref,
        "source_snapshot_sha256": _canonical_sha256(source_snapshot),
        "publication_boundary": {
            "raw_task_id_published": False,
            "owner_identity_published": False,
            "database_or_local_paths_published": False,
            "wgs84_coordinates_published": False,
            "prompt_or_model_response_text_published": False,
            "credentials_published": False,
            "local_coordinates_only": True,
            "source_snapshot_digest_is_attestation_not_public_recomputation": True,
        },
        "mission": {
            "backend_type": "px4_gazebo_sitl",
            "task_kind": str(task.get("kind") or ""),
            "task_status": str(task.get("status") or ""),
            "recovery_epoch_count": len(epochs),
        },
        "recovery_epochs": epochs,
        "telemetry": {
            "frame": "local_ned",
            "sample_count": len(profile),
            "raw_sample_count": replay.get("raw_sample_count"),
            "samples": profile,
            "read_only": True,
        },
        "terminal_observations": {
            "route_completed_claimed": waypoint_gate.get("route_completed_claimed"),
            "dropoff_verified": dropoff_gate.get("dropoff_verified"),
            "payload_release_observed_sim": payload_gate.get(
                "payload_release_observed_sim"
            ),
            "sitl_delivery_claimed": delivery_gate.get("sitl_delivery_claimed"),
            "return_progress_observed": monitor.get("return_progress_observed"),
            "landed": monitor.get("landed"),
            "delivery_completion_claimed": False,
            "physical_delivery_verified": False,
            "physical_execution_invoked": False,
        },
        "limitations": [
            "The source snapshot digest attests to the private source record but cannot be recomputed without that record.",
            "Local-coordinate telemetry is a bounded replay view, not proof of physical execution or delivery completion.",
        ],
        "bundle_generation_progress_counted": False,
    }
    if any(
        epoch.get("approval_and_dispatch", {}).get("evidence_kind")
        == "reference_only"
        for epoch in epochs
    ):
        bundle_without_hash["limitations"].append(
            "At least one historical approval is reference-only because the source task predates dispatch-receipt history preservation."
        )
    return {
        **bundle_without_hash,
        "bundle_sha256": _canonical_sha256(bundle_without_hash),
    }


def _forbidden_public_paths(value: Any, *, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            next_path = f"{path}.{key}"
            if str(key).lower() in _FORBIDDEN_PUBLIC_KEYS:
                findings.append(next_path)
            findings.extend(_forbidden_public_paths(item, path=next_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_forbidden_public_paths(item, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        lowered = value.lower()
        if (
            "/users/" in lowered
            or "/private/" in lowered
            or "/tmp/" in lowered
            or "file://" in lowered
            or "sk-" in lowered
            or re.search(r"\btask_[0-9a-f]{8,}\b", lowered)
        ):
            findings.append(path)
    return findings


def verify_anonymized_recovery_replay_bundle(
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    blocking_reasons: list[str] = []
    limitations: list[str] = []
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        blocking_reasons.append("replay_bundle_schema_not_supported")
    public_run_ref = str(bundle.get("public_run_ref") or "")
    if not _PUBLIC_RUN_REF_PATTERN.fullmatch(public_run_ref) or public_run_ref.startswith(
        "task_"
    ):
        blocking_reasons.append("replay_bundle_public_run_ref_invalid")
    expected_bundle_hash = str(bundle.get("bundle_sha256") or "")
    unhashed = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    if not expected_bundle_hash or expected_bundle_hash != _canonical_sha256(unhashed):
        blocking_reasons.append("replay_bundle_hash_mismatch")
    forbidden_paths = _forbidden_public_paths(bundle)
    if forbidden_paths:
        blocking_reasons.append("replay_bundle_publication_boundary_violated")

    verified_cycles = 0
    epoch_results: list[dict[str, Any]] = []
    for epoch in _sequence(bundle.get("recovery_epochs")):
        if not isinstance(epoch, Mapping):
            blocking_reasons.append("replay_bundle_epoch_not_object")
            continue
        epoch_reasons: list[str] = []
        proposal = _mapping(epoch.get("proposal"))
        intent = _mapping(proposal.get("recovery_intent"))
        compilation = _mapping(proposal.get("intent_compilation"))
        reachability = _mapping(proposal.get("reachability_verification"))
        observation = _mapping(epoch.get("observation"))
        outcome = _mapping(observation.get("outcome_verification"))
        binding = _mapping(epoch.get("approval_and_dispatch"))
        proposal_id = str(proposal.get("proposal_id") or "")
        if not recovery_artifact_hash_matches(intent, id_prefix="recovery_intent"):
            epoch_reasons.append("recovery_intent_hash_mismatch")
        if not recovery_artifact_hash_matches(
            compilation, id_prefix="recovery_compilation"
        ):
            epoch_reasons.append("recovery_compilation_hash_mismatch")
        if not recovery_artifact_hash_matches(
            reachability, id_prefix="recovery_reachability"
        ):
            epoch_reasons.append("recovery_reachability_hash_mismatch")
        if not recovery_artifact_hash_matches(
            outcome, id_prefix="recovery_outcome_verification"
        ):
            epoch_reasons.append("recovery_outcome_hash_mismatch")
        if (
            compilation.get("source_intent_id") != intent.get("recovery_intent_id")
            or compilation.get("source_intent_sha256")
            != intent.get("recovery_intent_sha256")
        ):
            epoch_reasons.append("intent_compilation_chain_mismatch")
        if (
            reachability.get("source_compilation_id")
            != compilation.get("recovery_compilation_id")
            or reachability.get("source_compilation_sha256")
            != compilation.get("recovery_compilation_sha256")
        ):
            epoch_reasons.append("compilation_reachability_chain_mismatch")
        if observation.get("source_proposal_id") != proposal_id:
            epoch_reasons.append("proposal_observation_chain_mismatch")

        if binding.get("evidence_kind") == "full_dispatch_receipt":
            receipt = _mapping(binding.get("receipt"))
            approval = _mapping(receipt.get("approval"))
            revalidation = _mapping(receipt.get("proposal_revalidation"))
            if not _receipt_hash_matches(receipt):
                epoch_reasons.append("published_dispatch_receipt_hash_mismatch")
            if revalidation.get("proposal_id") != proposal_id:
                epoch_reasons.append("proposal_dispatch_chain_mismatch")
            if revalidation.get("validation_status") != "valid":
                epoch_reasons.append("dispatch_revalidation_not_valid")
            if approval.get("operator_approval_performed") is not True:
                epoch_reasons.append("human_approval_not_observed")
            if approval.get("approval_id") != binding.get("approval_ref"):
                epoch_reasons.append("approval_reference_mismatch")
            if receipt.get("dispatch_authority_created") is not True:
                epoch_reasons.append("dispatch_authority_not_created")
            if receipt.get("recovery_action") != compilation.get("compiled_action"):
                epoch_reasons.append("approved_action_compilation_mismatch")
            if not _bounded_parameters_preserve_compilation(
                receipt.get("recovery_parameters"),
                compilation.get("compiled_parameters"),
            ):
                epoch_reasons.append("approved_parameters_compilation_mismatch")
            if not _bounded_parameters_preserve_compilation(
                approval.get("approved_parameters"),
                compilation.get("compiled_parameters"),
            ):
                epoch_reasons.append("approval_payload_compilation_mismatch")
        else:
            if not binding.get("approval_ref"):
                epoch_reasons.append("approval_reference_missing")
            limitations.append("historical_dispatch_receipt_not_preserved_in_source_task")

        if outcome.get("dispatch_authority_observed") is not True:
            epoch_reasons.append("outcome_dispatch_authority_not_observed")
        if outcome.get("command_ack_observed") is not True:
            epoch_reasons.append("command_ack_not_observed")
        if outcome.get("executor_effect_observed") is not True:
            epoch_reasons.append("executor_effect_not_observed")
        if outcome.get("target_reached") is not True:
            epoch_reasons.append("recovery_target_not_reached")
        if outcome.get("ack_is_execution_effect") is not False:
            epoch_reasons.append("ack_effect_boundary_not_preserved")
        if outcome.get("recovery_success_verified") is not True:
            epoch_reasons.append("recovery_success_not_verified")
        if outcome.get("resume_status") == "resumed_auto_mission":
            resume = _mapping(outcome.get("resume_safety_verification"))
            if (
                resume.get("verification_status") != "verified"
                or resume.get("resume_auto_authorized") is not True
            ):
                epoch_reasons.append("auto_resume_not_verified")
        if not epoch_reasons:
            verified_cycles += 1
        blocking_reasons.extend(
            f"epoch_{epoch.get('epoch_index')}:{reason}" for reason in epoch_reasons
        )
        epoch_results.append(
            {
                "epoch_index": epoch.get("epoch_index"),
                "proposal_id": proposal_id,
                "verification_status": "verified" if not epoch_reasons else "failed",
                "blocking_reasons": epoch_reasons,
            }
        )

    terminal = _mapping(bundle.get("terminal_observations"))
    if terminal.get("delivery_completion_claimed") is not False:
        blocking_reasons.append("delivery_completion_overclaimed")
    if terminal.get("physical_delivery_verified") is not False:
        blocking_reasons.append("physical_delivery_overclaimed")
    if terminal.get("physical_execution_invoked") is not False:
        blocking_reasons.append("physical_execution_overclaimed")
    if verified_cycles < 2:
        limitations.append("fewer_than_two_verified_recovery_cycles")

    unique_limitations = list(dict.fromkeys(limitations))
    status = "failed" if blocking_reasons else (
        "verified_with_limitations" if unique_limitations else "verified"
    )
    return {
        "schema_version": VERDICT_SCHEMA_VERSION,
        "public_run_ref": public_run_ref,
        "verification_status": status,
        "bundle_integrity_verified": not any(
            reason.startswith("replay_bundle_") for reason in blocking_reasons
        ),
        "closed_loop_cycle_count": verified_cycles,
        "causal_form": "Form 3" if verified_cycles >= 2 else "Form 2",
        "epoch_results": epoch_results,
        "blocking_reasons": blocking_reasons,
        "limitations": unique_limitations,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "VERDICT_SCHEMA_VERSION",
    "build_anonymized_recovery_replay_bundle",
    "verify_anonymized_recovery_replay_bundle",
]
