"""MissionOS cross-session lesson and curator projection.

This module connects source-bound MissionOS artifacts to bounded runtime
workers: Knowledge Curator subprocess execution, runtime policy/registry/table
application, and opt-in PX4/Gazebo SITL dispatch. It still must not schedule
background agents, perform automatic dispatch, grant hardware authority, claim
physical execution, or claim delivery completion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid
from typing import Any, Mapping

import yaml

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.gateway.missionos_capabilities import (
    approval_request_tool_record,
    capability_invocation_context,
)
from src.gateway.missionos_knowledge_browser import (
    build_missionos_knowledge_browser_summary,
)
from src.gateway.missionos_milestone import ARTIFACT_ROOT, _relative
from src.gateway.missionos_runtime_bridge import invoke_missionos_subprocess
from src.intelligence.llm_repair_planner import run_llm_repair_planner
from src.intelligence.llm_response_planner import run_llm_response_planner
from src.runtime.runtime_claim_evidence import (
    AUTHORITY_RUNTIME_CLAIM_KEYS,
    RuntimeClaimValidationError,
    normalize_runtime_claims,
    runtime_claim_validation_summary,
)
from src.runtime.missionos_sitl_dispatch_runtime import (
    WIND_FEED_FORWARD_MPS_ENV,
    WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV,
    WIND_PREEMPTIVE_OFFSET_M_ENV,
    form2a_backend_action_smoke_env,
    invoke_missionos_sitl_dispatch_runtime,
    runtime_summary_supports_dispatch,
)


LESSON_SCHEMA_VERSION = "cross_session_lesson.v1"
CURATOR_SCHEMA_VERSION = "missionos_knowledge_curator_dry_run.v1"
PRODUCTION_CURATOR_SCHEMA_VERSION = "missionos_knowledge_curator_run.v1"
ACTIVE_LESSON_INDEX_SCHEMA_VERSION = "missionos_active_lesson_index.v1"
SUMMARY_SCHEMA_VERSION = "missionos_knowledge_sharing_gui_summary.v1"
POLICY_UPDATE_CANDIDATE_SCHEMA_VERSION = "missionos_policy_update_candidate.v1"
OPERATOR_POLICY_APPROVAL_SCHEMA_VERSION = "missionos_operator_policy_approval_record.v1"
ACTIVE_POLICY_VERSION_SCHEMA_VERSION = "missionos_active_policy_version.v1"
AUTOMATIC_RECOVERY_RULE_SCHEMA_VERSION = "missionos_automatic_recovery_rule.v1"
BOUNDED_DISPATCH_AUTHORITY_SCHEMA_VERSION = "missionos_bounded_dispatch_authority.v1"
POLICY_AUTHORITY_SUMMARY_SCHEMA_VERSION = "missionos_policy_authority_gui_summary.v1"
OPERATOR_DISPATCH_APPROVAL_SCHEMA_VERSION = "missionos_operator_dispatch_approval_record.v1"
DETERMINISTIC_DISPATCH_GATE_SCHEMA_VERSION = "missionos_deterministic_dispatch_gate_result.v1"
BOUNDED_DISPATCH_EXECUTION_SCHEMA_VERSION = "missionos_bounded_dispatch_execution_receipt.v1"
BACKEND_ACTION_REQUEST_SCHEMA_VERSION = "missionos_backend_action_request.v1"
DISPATCH_OUTCOME_OBSERVATION_SCHEMA_VERSION = "missionos_dispatch_outcome_observation.v1"
RECOVERY_VERIFIER_SCHEMA_VERSION = "missionos_recovery_verifier_result.v1"
DISPATCH_AUDIT_RECORD_SCHEMA_VERSION = "missionos_bounded_dispatch_audit_record.v1"
SITL_DISPATCH_EXECUTION_SUMMARY_SCHEMA_VERSION = "missionos_sitl_dispatch_execution_gui_summary.v1"
SCOPED_FORM3_CLOSED_LOOP_SUMMARY_SCHEMA_VERSION = "missionos_scoped_form3_closed_loop_gui_summary.v1"
SCOPED_FORM3_CLOSED_LOOP_RECORD_SCHEMA_VERSION = "missionos_scoped_form3_closed_loop_record.v1"
FORM2A_RESPONSE_SELECTION_SCHEMA_VERSION = "missionos_form2a_response_selection.v1"
FORM2A_OPERATOR_APPROVAL_TOKEN_SCHEMA_VERSION = "missionos_form2a_operator_approval_token.v1"
FORM2A_HUMAN_OPERATOR_REVIEW_SCHEMA_VERSION = "missionos_form2a_human_operator_review.v1"
FORM2A_HUMAN_OPERATOR_REVIEW_SUMMARY_SCHEMA_VERSION = "missionos_form2a_human_operator_review_gui_summary.v1"
FORM2A_RESPONSE_SELECTION_SUMMARY_SCHEMA_VERSION = "missionos_form2a_response_selection_gui_summary.v1"
FORM2A_ACTION_CONSUMMATION_SCHEMA_VERSION = "missionos_form2a_action_consumption.v1"
FORM2A_ACTION_CONSUMMATION_SUMMARY_SCHEMA_VERSION = "missionos_form2a_action_consumption_gui_summary.v1"
LLM_REPAIR_PLANNER_SUMMARY_SCHEMA_VERSION = "missionos_llm_repair_planner_gui_summary.v1"
FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV = "RUN_MISSIONOS_FORM2A_TRAJECTORY_REOBSERVATION"
FORM2A_TRAJECTORY_REOBSERVATION_COMMAND_ENV = "MISSIONOS_FORM2A_TRAJECTORY_REOBSERVATION_COMMAND"
FORM2A_TRAJECTORY_REOBSERVATION_TIMEOUT_ENV = "MISSIONOS_FORM2A_TRAJECTORY_REOBSERVATION_TIMEOUT_SECONDS"
DEFAULT_FORM2A_REOBSERVATION_TIMEOUT_SECONDS = 900
FORM2A_COMPENSATION_RESPONSE_KINDS = frozenset(
    {
        "operator_gated_wind_replan_with_compensation",
        "operator_gated_wind_compensated_reroute",
    }
)
FORM2A_WARNING_RESPONSE_KIND = "operator_gated_continue_with_wind_warning"
FORM2A_PAYLOAD_RECOVERY_RESPONSE_KIND = "operator_gated_payload_recovery_land"
FORM2A_DEFAULT_IMPROVEMENT_GATE_RATIO = 1.0
INTERIM_RULE_INTELLIGENCE_SOURCE = "interim_rule_static_selector_pending_llm_migration"
GUARDRAIL_FALLBACK_INTELLIGENCE_SOURCE = "interim_rule_static_selector_fallback"
EXTERNAL_REVIEW_INTELLIGENCE_SOURCE = "external_claude_codex_session"
AI_AGENT_PROGRESS_ELIGIBLE_INTELLIGENCE_SOURCE = "llm_response_planner"
REPAIR_PLANNER_INTELLIGENCE_SOURCE = "llm_repair_planner"

MISSIONOS_RUNTIME_CLAIM_KEYS = AUTHORITY_RUNTIME_CLAIM_KEYS
REPO_ROOT = Path(__file__).resolve().parents[2]
FORM2A_IMPROVEMENT_GATE_CONFIG = REPO_ROOT / "config" / "form1_runtime_audit_thresholds.yaml"
KNOWLEDGE_CURATOR_WORKER = REPO_ROOT / "scripts" / "missionos_knowledge_curator_worker.py"
POLICY_RUNTIME_WORKER = REPO_ROOT / "scripts" / "missionos_policy_runtime_worker.py"
PAYLOAD_RECOVERY_ACTION_AUDIT = (
    REPO_ROOT / "scripts" / "audit_mission_designer_payload_recovery_action.py"
)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(payload), sort_keys=True).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _policy_runtime_source_projection(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": policy.get("schema_version"),
        "policy_version_id": policy.get("policy_version_id"),
        "policy_update_candidate_ref": policy.get("policy_update_candidate_ref"),
        "approval_ref": policy.get("approval_ref"),
        "rollback_ref": policy.get("rollback_ref"),
        "source_lesson_ref": policy.get("source_lesson_ref"),
        "operator_approval_required": policy.get("operator_approval_required") is True,
    }


def _rule_runtime_source_projection(rule: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": rule.get("schema_version"),
        "recovery_rule_id": rule.get("recovery_rule_id"),
        "active_policy_ref": rule.get("active_policy_ref"),
        "bounded_action_ref": rule.get("bounded_action_ref"),
        "recommended_action": rule.get("recommended_action"),
        "operator_approval_required": rule.get("operator_approval_required") is True,
        "automatic_dispatch_suppressed": rule.get("automatic_dispatch_suppressed") is True,
    }


def _authority_runtime_source_projection(authority: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": authority.get("schema_version"),
        "dispatch_authority_id": authority.get("dispatch_authority_id"),
        "dispatch_ref": authority.get("dispatch_ref"),
        "active_policy_ref": authority.get("active_policy_ref"),
        "automatic_recovery_rule_ref": authority.get("automatic_recovery_rule_ref"),
        "approval_ref": authority.get("approval_ref"),
        "bounded_action_ref": authority.get("bounded_action_ref"),
        "operator_approval_required": authority.get("operator_approval_required") is True,
        "automatic_dispatch_suppressed": authority.get("automatic_dispatch_suppressed") is True,
    }


def _read_json_sha256(path_value: Any) -> tuple[dict[str, Any], str]:
    path_text = str(path_value or "")
    payload = _read_json(Path(path_text)) if path_text else None
    mapping = payload if isinstance(payload, dict) else {}
    return mapping, _sha256_json(mapping) if mapping else ""


def _latest_payloads(root: Path, filename: str, *, limit: int = 5) -> list[tuple[str, dict[str, Any]]]:
    if not root.exists():
        return []
    paths = sorted(
        root.rglob(filename),
        key=lambda path: (path.stat().st_mtime, path.as_posix()),
        reverse=True,
    )
    payloads: list[tuple[str, dict[str, Any]]] = []
    for path in paths:
        payload = _read_json(path)
        if payload is not None:
            payloads.append((_relative(path), payload))
        if len(payloads) >= limit:
            break
    return payloads


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _float_values_match(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return False


def _llm_response_parameters_bound_to_runtime_env(
    parameters: Mapping[str, Any],
    dispatch_env: Mapping[str, Any],
    reobservation_env: Mapping[str, Any],
) -> bool:
    """Return true only when captured runtime envs contain the LLM parameters."""

    checks = {
        "direction_deg": WIND_PREEMPTIVE_OFFSET_DIRECTION_DEG_ENV,
        "feed_forward_mps": WIND_FEED_FORWARD_MPS_ENV,
        "preemptive_offset_m": WIND_PREEMPTIVE_OFFSET_M_ENV,
    }
    required = {
        key: env_key
        for key, env_key in checks.items()
        if parameters.get(key) is not None
    }
    if not required or not dispatch_env or not reobservation_env:
        return False
    return all(
        _float_values_match(parameters[key], env.get(env_key))
        for key, env_key in required.items()
        for env in (dispatch_env, reobservation_env)
    )


def _llm_payload_parameters_bound_to_runtime_evidence(
    parameters: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> bool:
    if parameters.get("payload_mass_kg") is None:
        return False
    observed = _as_mapping(evidence.get("form2a_backend_action_parameters"))
    return _float_values_match(parameters.get("payload_mass_kg"), observed.get("payload_mass_kg"))


def _resolve_artifact_path(root: Path, artifact_path: str) -> Path:
    path = Path(artifact_path)
    if path.is_absolute() or path.exists():
        return path
    marker = "output/mission_designer_behavior_delta_audits/"
    if marker in artifact_path:
        return root / artifact_path.split(marker, 1)[-1]
    return root / artifact_path


def _resolve_repo_or_artifact_path(root: Path, artifact_path: str | Path) -> Path:
    path = Path(artifact_path)
    if path.is_absolute() or path.exists():
        return path
    repo_path = REPO_ROOT / path
    if repo_path.exists():
        return repo_path
    return _resolve_artifact_path(root, path.as_posix())


def _form2a_improvement_gate(root: Path) -> dict[str, Any]:
    """Load the noise-floor gate that decides whether Form 2a improved flight."""

    blocking_reasons: list[str] = []
    try:
        config = yaml.safe_load(FORM2A_IMPROVEMENT_GATE_CONFIG.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        config = {}
        blocking_reasons.append("form2a_improvement_gate_config_unreadable")
    gate = _as_mapping(
        _as_mapping(_as_mapping(config).get("thresholds"))
        .get("px4_gazebo_wind_trajectory_delta", {})
        .get("form2a_improvement_gate")
    )
    try:
        ratio = float(gate.get("ratio"))
    except (TypeError, ValueError):
        ratio = FORM2A_DEFAULT_IMPROVEMENT_GATE_RATIO
        blocking_reasons.append("form2a_improvement_gate_ratio_missing")
    if ratio <= 0.0 or ratio >= 1.0:
        blocking_reasons.append("form2a_improvement_gate_ratio_out_of_range")

    source_text = str(gate.get("noise_floor_source") or "")
    source_path = _resolve_repo_or_artifact_path(root, source_text) if source_text else Path("")
    expected_sha = str(gate.get("noise_floor_source_sha256") or "")
    observed_sha = ""
    if not source_text:
        blocking_reasons.append("form2a_improvement_gate_noise_floor_source_missing")
    elif not source_path.exists():
        blocking_reasons.append("form2a_improvement_gate_noise_floor_source_not_found")
    else:
        observed_sha = _sha256_file(source_path)
        if observed_sha != expected_sha:
            blocking_reasons.append("form2a_improvement_gate_noise_floor_source_sha256_mismatch")

    try:
        sample_count = int(gate.get("sample_count") or 0)
    except (TypeError, ValueError):
        sample_count = 0
    if sample_count <= 0:
        blocking_reasons.append("form2a_improvement_gate_sample_count_missing")
    try:
        noise_fraction_95 = float(gate.get("noise_fraction_95"))
    except (TypeError, ValueError):
        noise_fraction_95 = 0.0
        blocking_reasons.append("form2a_improvement_gate_noise_fraction_missing")

    return {
        "ratio": ratio,
        "noise_floor_source": source_text,
        "noise_floor_source_artifact_sha256": observed_sha,
        "noise_floor_source_artifact_expected_sha256": expected_sha,
        "noise_floor_source_artifact_sha256_matches": bool(
            observed_sha and expected_sha and observed_sha == expected_sha
        ),
        "sample_count": sample_count,
        "noise_fraction_95": noise_fraction_95,
        "config_path": _repo_or_path_relative(FORM2A_IMPROVEMENT_GATE_CONFIG),
        "supported": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
    }


def _repo_or_path_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return _relative(path)


def _claim_payload(payload: Mapping[str, Any], *, validate_progress: bool = True) -> dict[str, Any]:
    return normalize_runtime_claims(
        payload,
        claim_keys=MISSIONOS_RUNTIME_CLAIM_KEYS,
        validate_progress=validate_progress,
    )


def _write_artifact(path: Path, payload: Mapping[str, Any], *, validate_progress: bool = True) -> dict[str, Any]:
    artifact = _claim_payload(payload, validate_progress=validate_progress)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    return artifact


def _attach_capability_context_to_artifact(
    *,
    root: Path,
    artifact_path: str,
    capability_context: Mapping[str, Any],
) -> None:
    if not artifact_path:
        return
    path = _resolve_artifact_path(root, artifact_path)
    payload = _read_json(path)
    if not payload:
        return
    payload["capability_invocation"] = dict(capability_context)
    payload["capability_invocation_ref"] = capability_context.get(
        "capability_invocation_ref", ""
    )
    payload["operator_facing_route"] = capability_context.get(
        "operator_facing_route", ""
    )
    payload["requested_by"] = capability_context.get("requested_by", "")
    _write_artifact(path, payload, validate_progress=False)


def _write_approval_request_tool_artifact(
    *,
    root: Path,
    approval_request_tool: Mapping[str, Any],
) -> str:
    approval_request_id = str(approval_request_tool.get("approval_request_id") or "")
    filename = (
        f"{approval_request_id}.json"
        if approval_request_id
        else "missionos_approval_request_tool.json"
    )
    path = _artifact_dir(root, "missionos_approval_request_tool") / filename
    payload = dict(approval_request_tool)
    payload["approval_request_artifact_path"] = _relative(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_artifact(path, payload, validate_progress=False)
    return _relative(path)


def _atomic_write_raw_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _missionos_worker_env() -> dict[str, str]:
    env: dict[str, str] = {}
    current = os.environ.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{REPO_ROOT}{os.pathsep}{current}" if current else str(REPO_ROOT)
    return env


def _evidence_fields(runtime_payload: Mapping[str, Any]) -> dict[str, Any]:
    evidence = runtime_payload.get("runtime_invocation_evidence")
    return {"runtime_invocation_evidence": evidence} if isinstance(evidence, Mapping) else {}


def _runtime_summary_artifact_path(payload: Mapping[str, Any]) -> str:
    direct = payload.get("runtime_summary_artifact_path")
    if isinstance(direct, str) and direct:
        return direct
    evidence = _as_mapping(payload.get("runtime_invocation_evidence"))
    nested = evidence.get("runtime_summary_artifact_path")
    return str(nested) if nested else ""


def _independent_runtime_dispatch_check(
    *,
    execution: Mapping[str, Any],
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    summary_path = _runtime_summary_artifact_path(execution)
    summary = _read_json(Path(summary_path)) if summary_path else None
    summary_payload = summary if isinstance(summary, Mapping) else {}
    runtime_evidence = _as_mapping(execution.get("runtime_invocation_evidence"))
    summary_source = str(
        execution.get("runtime_summary_source")
        or runtime_evidence.get("runtime_summary_source")
        or ""
    )
    summary_source_path = str(
        execution.get("runtime_summary_source_path")
        or runtime_evidence.get("runtime_summary_source_path")
        or ""
    )
    source_summary_payload: Mapping[str, Any] = {}
    if summary_source == "smoke_summary_json" and summary_source_path:
        source_summary = _read_json(Path(summary_source_path))
        source_summary_payload = source_summary if isinstance(source_summary, Mapping) else {}
    source_summary_matches = bool(
        summary_source != "smoke_summary_json"
        or (source_summary_payload and dict(source_summary_payload) == dict(summary_payload))
    )
    supports_dispatch = runtime_summary_supports_dispatch(dict(summary_payload))
    return {
        "schema_version": "missionos_independent_runtime_verifier_check.v1",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "check_kind": "runtime_summary_artifact_reload",
        "runtime_summary_artifact_path": summary_path,
        "runtime_summary_reloaded": bool(summary_payload),
        "runtime_summary_sha256": _sha256_json(summary_payload) if summary_payload else "",
        "runtime_summary_source": summary_source,
        "runtime_summary_source_path": summary_source_path,
        "runtime_summary_source_reloaded": bool(source_summary_payload),
        "runtime_summary_source_sha256": (
            _sha256_json(source_summary_payload) if source_summary_payload else ""
        ),
        "runtime_summary_source_matches_runtime_summary": source_summary_matches,
        "runtime_summary_supports_dispatch": supports_dispatch,
        "outcome_observed_in_runtime": outcome.get("outcome_observed_in_runtime") is True,
        "dispatch_ref": execution.get("dispatch_ref"),
        "verifier_passed": bool(
            supports_dispatch
            and source_summary_matches
            and outcome.get("outcome_observed_in_runtime") is True
        ),
    }


def _runtime_claims(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    try:
        return _claim_payload(payload, validate_progress=True), []
    except RuntimeClaimValidationError as exc:
        try:
            normalized = _claim_payload(payload, validate_progress=False)
        except RuntimeClaimValidationError:
            normalized = dict(payload)
        return normalized, [str(exc)]


def _choose_source_card(knowledge: Mapping[str, Any]) -> Mapping[str, Any]:
    cards = [card for card in _as_list(knowledge.get("cards")) if isinstance(card, Mapping)]
    cards = sorted(cards, key=_source_card_sort_key, reverse=True)
    return cards[0] if cards else {}


def _source_card_sort_key(card: Mapping[str, Any]) -> tuple[float, str]:
    artifact_path = str(card.get("artifact_path") or "")
    try:
        mtime = Path(artifact_path).stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, artifact_path)


def _source_card_persistable(card: Mapping[str, Any]) -> bool:
    failure_mode_id = str(card.get("failure_mode_id") or "")
    artifact_path = str(card.get("artifact_path") or "")
    if not artifact_path:
        return False
    if failure_mode_id in {"", "artifact_unreadable", "no_failure_mode_available"}:
        return False
    return True


def _artifact_dir(root: Path, prefix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return root / f"{prefix}_{stamp}_{uuid.uuid4().hex[:12]}"


def _source_context(
    *,
    artifact_root: Path,
    live_run_root: Path | str | None = None,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    knowledge_kwargs: dict[str, Any] = {"artifact_root": artifact_root}
    if live_run_root is not None:
        knowledge_kwargs["live_run_root"] = live_run_root
    knowledge = build_missionos_knowledge_browser_summary(**knowledge_kwargs)
    source_card = _choose_source_card(knowledge)
    next_inspection = _as_mapping(knowledge.get("next_inspection"))
    return knowledge, source_card, next_inspection


def _lesson_fields_from_source(
    source_card: Mapping[str, Any],
    next_inspection: Mapping[str, Any],
) -> tuple[str, str, str]:
    failure_mode_id = str(
        source_card.get("failure_mode_id")
        or next_inspection.get("failure_mode_id")
        or "no_failure_mode_available"
    )
    source_artifact = str(source_card.get("artifact_path") or next_inspection.get("artifact_path") or "")
    recommended_next = str(
        source_card.get("recommended_next_inspection")
        or next_inspection.get("recommended_next_inspection")
        or "No source knowledge card was available."
    )
    return failure_mode_id, source_artifact, recommended_next


def _persist_lesson(
    *,
    root: Path,
    generated_at: str,
    failure_mode_id: str,
    source_artifact: str,
    source_status: Any,
    recommended_next: str,
    lesson_status: str,
    reuse_scope: str,
    production_reflected: bool,
) -> tuple[Path, dict[str, Any]]:
    lesson_dir = _artifact_dir(root, "cross_session_lesson")
    lesson_path = lesson_dir / "cross_session_lesson.json"
    lesson = {
        "schema_version": LESSON_SCHEMA_VERSION,
        "lesson_id": f"cross_session_lesson_{uuid.uuid4().hex[:12]}",
        "lesson_status": lesson_status,
        "causal_form": "Form 0b",
        "progress_counted": False,
        "generated_at": generated_at,
        "source_failure_mode_id": failure_mode_id,
        "source_knowledge_card_status": source_status,
        "source_artifact_path": source_artifact,
        "recommended_next_inspection": recommended_next,
        "reuse_scope": reuse_scope,
        "applies_to_future_runs": True,
        "production_reflected": production_reflected,
        "policy_update_applied": False,
        "automatic_recovery_rule_created": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "public_sync_performed": False,
    }
    lesson_dir.mkdir(parents=True, exist_ok=True)
    lesson = _write_artifact(lesson_path, lesson)
    return lesson_path, lesson


def run_knowledge_curator_dry_run(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    live_run_root: Path | str | None = None,
) -> dict[str, Any]:
    """Persist a cross-session lesson candidate and curator dry-run receipt."""

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    knowledge, source_card, next_inspection = _source_context(
        artifact_root=root,
        live_run_root=live_run_root,
    )
    if not _source_card_persistable(source_card):
        return build_missionos_knowledge_sharing_summary(artifact_root=root)
    failure_mode_id, source_artifact, recommended_next = _lesson_fields_from_source(
        source_card,
        next_inspection,
    )
    generated_at = datetime.now(timezone.utc).isoformat()

    lesson_path, lesson = _persist_lesson(
        root=root,
        generated_at=generated_at,
        failure_mode_id=failure_mode_id,
        source_artifact=source_artifact,
        source_status=source_card.get("status") or next_inspection.get("status"),
        recommended_next=recommended_next,
        lesson_status="persisted_candidate",
        reuse_scope="cross_session_diagnostic_read_only",
        production_reflected=False,
    )

    curator_dir = _artifact_dir(root, "knowledge_curator_dry_run")
    curator_path = curator_dir / "knowledge_curator_dry_run.json"
    curator = {
        "schema_version": CURATOR_SCHEMA_VERSION,
        "curator_run_id": f"knowledge_curator_dry_run_{uuid.uuid4().hex[:12]}",
        "curator_status": "completed",
        "causal_form": "Form 0b",
        "progress_counted": False,
        "generated_at": generated_at,
        "agent_role": "knowledge_curator",
        "dry_run_only": True,
        "dry_run_agent_execution_started": True,
        "agent_execution_started": False,
        "no_background_automation": True,
        "background_work_scheduled": False,
        "source_knowledge_browser_status": knowledge.get("browser_status"),
        "source_failure_mode_id": failure_mode_id,
        "source_artifact_path": source_artifact,
        "cross_session_lesson_ref": f"cross_session_lesson:{lesson['lesson_id']}",
        "cross_session_lesson_artifact_path": _relative(lesson_path),
        "policy_update_applied": False,
        "automatic_recovery_rule_created": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "public_sync_performed": False,
    }
    curator_dir.mkdir(parents=True, exist_ok=True)
    _write_artifact(curator_path, curator)

    return build_missionos_knowledge_sharing_summary(artifact_root=root)


def run_knowledge_curator_production_publish(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    live_run_root: Path | str | None = None,
) -> dict[str, Any]:
    """Publish the current diagnostic lesson into the active knowledge index.

    This is the non-dry-run L3/L4 success path. It records a bounded,
    operator-requested Knowledge Curator run and an active lesson index update,
    while keeping policy updates, automatic recovery rules, dispatch, physical
    execution, delivery completion, and public sync false.
    """

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    knowledge, source_card, next_inspection = _source_context(
        artifact_root=root,
        live_run_root=live_run_root,
    )
    if not _source_card_persistable(source_card):
        return build_missionos_knowledge_sharing_summary(artifact_root=root)
    failure_mode_id, source_artifact, recommended_next = _lesson_fields_from_source(
        source_card,
        next_inspection,
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    lesson_path, lesson = _persist_lesson(
        root=root,
        generated_at=generated_at,
        failure_mode_id=failure_mode_id,
        source_artifact=source_artifact,
        source_status=source_card.get("status") or next_inspection.get("status"),
        recommended_next=recommended_next,
        lesson_status="active_diagnostic_lesson",
        reuse_scope="cross_session_diagnostic_active_index",
        production_reflected=True,
    )

    approval_dir = _artifact_dir(root, "knowledge_curator_operator_approval")
    approval_path = approval_dir / "knowledge_curator_operator_approval_record.json"
    approval = {
        "schema_version": "missionos_knowledge_curator_operator_approval_record.v1",
        "approval_id": f"knowledge_curator_operator_approval_{uuid.uuid4().hex[:12]}",
        "approval_status": "approved",
        "generated_at": generated_at,
        "approval_scope": "publish_cross_session_lesson_to_active_index",
        "lesson_ref": f"cross_session_lesson:{lesson['lesson_id']}",
        "lesson_artifact_path": _relative(lesson_path),
        "operator_approved_in_artifact": True,
        "agent_execution_started": False,
        "dispatch_executed": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
    }
    approval_dir.mkdir(parents=True, exist_ok=True)
    approval = _write_artifact(approval_path, approval)

    index_dir = _artifact_dir(root, "active_lesson_index")
    index_path = index_dir / "active_lesson_index.json"
    curator_dir = _artifact_dir(root, "knowledge_curator_run")
    curator_path = curator_dir / "knowledge_curator_run.json"
    runtime_dir = _artifact_dir(root, "knowledge_curator_runtime") / f"run_{uuid.uuid4().hex[:12]}"
    runtime_payload = invoke_missionos_subprocess(
        [
            sys.executable,
            str(KNOWLEDGE_CURATOR_WORKER),
            "--lesson-path",
            str(lesson_path.resolve()),
            "--lesson-artifact-path",
            _relative(lesson_path),
            "--active-index-path",
            str(index_path.resolve()),
            "--curator-run-path",
            str(curator_path.resolve()),
            "--operator-approval-ref",
            f"knowledge_curator_operator_approval:{approval['approval_id']}",
            "--operator-approval-artifact-path",
            _relative(approval_path),
            "--generated-at",
            generated_at,
        ],
        invocation_target="scripts/missionos_knowledge_curator_worker.py",
        artifact_dir=runtime_dir,
        backend_target="missionos_knowledge_curator",
        cwd=REPO_ROOT,
        env=_missionos_worker_env(),
    )
    runtime_evidence = _evidence_fields(runtime_payload)
    active_index = _read_json(index_path) or {}
    curator = _read_json(curator_path) or {}
    runtime_success = bool(
        _as_mapping(runtime_payload.get("runtime_invocation_evidence")).get("invocation_exit_code") == 0
        and active_index
        and curator
    )
    if active_index:
        active_index.update(
            {
                "knowledge_index_updated_in_artifact": runtime_success,
                "knowledge_index_updated_in_runtime": runtime_success,
                "agent_runtime_ref": runtime_payload.get("runtime_evidence_artifact_path"),
                "worker_process_pid": _as_mapping(runtime_evidence.get("runtime_invocation_evidence")).get(
                    "process_pid"
                ),
                **runtime_evidence,
            }
        )
        active_index = _write_artifact(index_path, active_index)
    if curator:
        curator.update(
            {
                "source_knowledge_browser_status": knowledge.get("browser_status"),
                "active_lesson_index_artifact_path": _relative(index_path),
                "operator_approval_artifact_path": _relative(approval_path),
                "operator_approved_in_artifact": runtime_success,
                "operator_approval_ref_consumed": runtime_success,
                "agent_execution_started_in_artifact": runtime_success,
                "knowledge_index_updated_in_artifact": runtime_success,
                "agent_execution_started_in_runtime": runtime_success,
                "operator_approved_in_runtime": runtime_success,
                "knowledge_index_updated_in_runtime": runtime_success,
                "agent_runtime_ref": runtime_payload.get("runtime_evidence_artifact_path"),
                "worker_process_pid": _as_mapping(runtime_evidence.get("runtime_invocation_evidence")).get(
                    "process_pid"
                ),
                "runtime_stdout_tail": runtime_payload.get("runtime_stdout_tail", ""),
                "runtime_stderr_tail": runtime_payload.get("runtime_stderr_tail", ""),
                **runtime_evidence,
            }
        )
        _write_artifact(curator_path, curator)

    return build_missionos_knowledge_sharing_summary(artifact_root=root)


def _latest_active_index(root: Path) -> tuple[str, dict[str, Any]]:
    active_indexes = _latest_payloads(root, "active_lesson_index.json")
    return active_indexes[0] if active_indexes else ("", {})


def _active_index_source_bound(root: Path, active_index: Mapping[str, Any]) -> bool:
    lesson_path = str(active_index.get("lesson_artifact_path") or "")
    if not lesson_path:
        return False
    lesson = _read_json(_resolve_artifact_path(root, lesson_path))
    if not lesson:
        return False
    return bool(
        active_index.get("lesson_ref") == f"cross_session_lesson:{lesson.get('lesson_id')}"
        and active_index.get("source_artifact_path") == lesson.get("source_artifact_path")
        and lesson.get("production_reflected") is True
    )


def run_policy_authority_promotion(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Create an operator-gated policy -> recovery -> dispatch authority path.

    The path creates authority artifacts, but it does not execute dispatch or
    touch PX4/Gazebo/MAVLink/actuators. Dispatch remains operator-gated.
    """

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    active_index_path, active_index = _latest_active_index(root)
    if active_index.get("index_status") != "updated" or not _active_index_source_bound(root, active_index):
        return build_policy_authority_summary(artifact_root=root)

    generated_at = datetime.now(timezone.utc).isoformat()
    lesson_ref = str(active_index.get("lesson_ref") or "")
    lesson_artifact_path = str(active_index.get("lesson_artifact_path") or "")
    source_failure_mode_id = str(active_index.get("source_failure_mode_id") or "unknown_failure_mode")
    policy_candidate_dir = _artifact_dir(root, "policy_update_candidate")
    policy_candidate_path = policy_candidate_dir / "policy_update_candidate.json"
    policy_candidate = {
        "schema_version": POLICY_UPDATE_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": f"policy_update_candidate_{uuid.uuid4().hex[:12]}",
        "candidate_status": "operator_review_ready",
        "causal_form": "Form 2a candidate",
        "progress_counted": False,
        "generated_at": generated_at,
        "source_active_lesson_index_ref": f"active_lesson_index:{active_index.get('index_id')}",
        "source_active_lesson_index_artifact_path": active_index_path,
        "source_lesson_ref": lesson_ref,
        "source_lesson_artifact_path": lesson_artifact_path,
        "source_failure_mode_id": source_failure_mode_id,
        "policy_update_kind": "diagnostic_recovery_recommendation",
        "rollback_required": True,
        "rollback_ref": f"policy_rollback_plan:{uuid.uuid4().hex[:12]}",
        "operator_approval_required": True,
        "llm_gate_judge_used": False,
        "approval_free_stronger_execution": False,
        "core_direct_execution_used": False,
        "dispatch_executed": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "public_sync_performed": False,
    }
    policy_candidate_dir.mkdir(parents=True, exist_ok=True)
    policy_candidate = _write_artifact(policy_candidate_path, policy_candidate)

    approval_dir = _artifact_dir(root, "operator_policy_approval")
    approval_path = approval_dir / "operator_policy_approval_record.json"
    approval = {
        "schema_version": OPERATOR_POLICY_APPROVAL_SCHEMA_VERSION,
        "approval_id": f"operator_policy_approval_{uuid.uuid4().hex[:12]}",
        "approval_status": "approved",
        "generated_at": generated_at,
        "policy_update_candidate_ref": f"policy_update_candidate:{policy_candidate['candidate_id']}",
        "policy_update_candidate_artifact_path": _relative(policy_candidate_path),
        "operator_approved_in_artifact": True,
        "approval_scope": "activate_policy_for_operator_gated_recovery_recommendation",
        "operator_approval_required": True,
        "approval_free_stronger_execution": False,
        "dispatch_executed": False,
        "automatic_dispatch_executed": False,
    }
    approval_dir.mkdir(parents=True, exist_ok=True)
    approval = _write_artifact(approval_path, approval)

    active_policy_dir = _artifact_dir(root, "active_policy_version")
    active_policy_path = active_policy_dir / "active_policy_version.json"
    active_policy = {
        "schema_version": ACTIVE_POLICY_VERSION_SCHEMA_VERSION,
        "policy_version_id": f"active_policy_version_{uuid.uuid4().hex[:12]}",
        "policy_status": "active",
        "generated_at": generated_at,
        "policy_update_applied_in_artifact": True,
        "policy_update_candidate_ref": f"policy_update_candidate:{policy_candidate['candidate_id']}",
        "policy_update_candidate_artifact_path": _relative(policy_candidate_path),
        "approval_ref": f"operator_policy_approval:{approval['approval_id']}",
        "approval_artifact_path": _relative(approval_path),
        "rollback_ref": policy_candidate["rollback_ref"],
        "source_lesson_ref": lesson_ref,
        "operator_approval_required": True,
        "llm_gate_judge_used": False,
        "approval_free_stronger_execution": False,
        "automatic_recovery_rule_created": False,
        "dispatch_authority_created": False,
        "dispatch_executed": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "public_sync_performed": False,
    }
    active_policy_dir.mkdir(parents=True, exist_ok=True)
    active_policy = _write_artifact(active_policy_path, active_policy)

    rule_dir = _artifact_dir(root, "automatic_recovery_rule")
    rule_path = rule_dir / "automatic_recovery_rule.json"
    bounded_action_ref = f"bounded_recovery_action:{uuid.uuid4().hex[:12]}"
    rule = {
        "schema_version": AUTOMATIC_RECOVERY_RULE_SCHEMA_VERSION,
        "recovery_rule_id": f"automatic_recovery_rule_{uuid.uuid4().hex[:12]}",
        "rule_status": "created_operator_gated",
        "generated_at": generated_at,
        "automatic_recovery_rule_created_in_artifact": True,
        "active_policy_ref": f"active_policy_version:{active_policy['policy_version_id']}",
        "active_policy_artifact_path": _relative(active_policy_path),
        "source_failure_mode_id": source_failure_mode_id,
        "recommended_action": "inspect_failure_receipt_then_operator_gated_recovery",
        "bounded_action_ref": bounded_action_ref,
        "operator_approval_required": True,
        "automatic_dispatch_suppressed": True,
        "dispatch_executed": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "public_sync_performed": False,
    }
    rule_dir.mkdir(parents=True, exist_ok=True)
    rule = _write_artifact(rule_path, rule)

    authority_dir = _artifact_dir(root, "bounded_dispatch_authority")
    authority_path = authority_dir / "bounded_dispatch_authority.json"
    dispatch_ref = f"bounded_dispatch_authority:{uuid.uuid4().hex[:12]}"
    authority = {
        "schema_version": BOUNDED_DISPATCH_AUTHORITY_SCHEMA_VERSION,
        "dispatch_authority_id": dispatch_ref.split(":", 1)[-1],
        "authority_status": "created_operator_gated",
        "generated_at": generated_at,
        "dispatch_authority_created_in_artifact": True,
        "dispatch_authority_kind": "operator_gated_bounded_path",
        "active_policy_ref": f"active_policy_version:{active_policy['policy_version_id']}",
        "active_policy_artifact_path": _relative(active_policy_path),
        "automatic_recovery_rule_ref": f"automatic_recovery_rule:{rule['recovery_rule_id']}",
        "automatic_recovery_rule_artifact_path": _relative(rule_path),
        "approval_ref": f"operator_policy_approval:{approval['approval_id']}",
        "approval_artifact_path": _relative(approval_path),
        "bounded_action_ref": bounded_action_ref,
        "dispatch_ref": dispatch_ref,
        "operator_approval_required": True,
        "automatic_dispatch_suppressed": True,
        "dispatch_executed": False,
        "automatic_dispatch_executed": False,
        "core_direct_execution_used": False,
        "llm_gate_judge_used": False,
        "approval_free_stronger_execution": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "public_sync_performed": False,
    }
    authority_dir.mkdir(parents=True, exist_ok=True)
    authority = _write_artifact(authority_path, authority)

    runtime_dir = _artifact_dir(root, "policy_authority_runtime") / f"run_{uuid.uuid4().hex[:12]}"
    runtime_state_dir = runtime_dir / "state"
    runtime_payload = invoke_missionos_subprocess(
        [
            sys.executable,
            str(POLICY_RUNTIME_WORKER),
            "--active-policy-path",
            str(active_policy_path.resolve()),
            "--active-policy-artifact-path",
            _relative(active_policy_path),
            "--recovery-rule-path",
            str(rule_path.resolve()),
            "--recovery-rule-artifact-path",
            _relative(rule_path),
            "--dispatch-authority-path",
            str(authority_path.resolve()),
            "--dispatch-authority-artifact-path",
            _relative(authority_path),
            "--runtime-state-dir",
            str(runtime_state_dir.resolve()),
            "--source-failure-mode-id",
            source_failure_mode_id,
        ],
        invocation_target="scripts/missionos_policy_runtime_worker.py",
        artifact_dir=runtime_dir,
        backend_target="missionos_policy_authority_runtime",
        cwd=REPO_ROOT,
        env=_missionos_worker_env(),
    )
    runtime_result = _as_mapping(runtime_payload.get("runtime_stdout_json"))
    runtime_evidence = _evidence_fields(runtime_payload)
    runtime_success = bool(
        _as_mapping(runtime_payload.get("runtime_invocation_evidence")).get("invocation_exit_code") == 0
        and runtime_result.get("policy_loaded") is True
        and runtime_result.get("policy_loaded_from_empty_baseline") is True
        and runtime_result.get("runtime_registry_hashes_match") is True
        and runtime_result.get("registered_rule_ref")
        and runtime_result.get("selected_rule_ref")
        and runtime_result.get("dispatch_authority_lookup_status") == "found"
    )
    runtime_common = {
        "runtime_worker_status": runtime_result.get("worker_status"),
        "runtime_state_dir": str(runtime_state_dir),
        "policy_engine_state_path": runtime_result.get("policy_engine_state_path"),
        "policy_engine_replay_path": runtime_result.get("policy_engine_replay_path"),
        "recovery_rule_registry_state_path": runtime_result.get("recovery_rule_registry_state_path"),
        "dispatch_authority_table_state_path": runtime_result.get(
            "dispatch_authority_table_state_path"
        ),
        "active_policy_source_projection_sha256": runtime_result.get(
            "active_policy_source_projection_sha256"
        ),
        "policy_engine_active_policy_source_projection_sha256": runtime_result.get(
            "policy_engine_active_policy_source_projection_sha256"
        ),
        "policy_engine_state_sha256": runtime_result.get("policy_engine_state_sha256"),
        "policy_engine_replay_sha256": runtime_result.get("policy_engine_replay_sha256"),
        "recovery_rule_source_projection_sha256": runtime_result.get(
            "recovery_rule_source_projection_sha256"
        ),
        "registry_rule_source_projection_sha256": runtime_result.get(
            "registry_rule_source_projection_sha256"
        ),
        "recovery_rule_registry_state_sha256": runtime_result.get(
            "recovery_rule_registry_state_sha256"
        ),
        "dispatch_authority_source_projection_sha256": runtime_result.get(
            "dispatch_authority_source_projection_sha256"
        ),
        "authority_table_source_projection_sha256": runtime_result.get(
            "authority_table_source_projection_sha256"
        ),
        "dispatch_authority_table_entry_state_sha256": runtime_result.get(
            "dispatch_authority_table_entry_state_sha256"
        ),
        "dispatch_authority_table_state_sha256": runtime_result.get(
            "dispatch_authority_table_state_sha256"
        ),
        "runtime_registry_hashes_match": runtime_result.get("runtime_registry_hashes_match")
        is True,
        "runtime_stdout_tail": runtime_payload.get("runtime_stdout_tail", ""),
        "runtime_stderr_tail": runtime_payload.get("runtime_stderr_tail", ""),
        **runtime_evidence,
    }
    active_policy.update(
        {
            "policy_status": "active_runtime_loaded" if runtime_success else active_policy.get("policy_status"),
            "policy_update_applied_in_runtime": runtime_success,
            "policy_engine_loaded_from_empty_baseline": runtime_result.get(
                "policy_loaded_from_empty_baseline"
            )
            is True,
            "policy_replay_baseline_state_kind": runtime_result.get(
                "policy_replay_baseline_state_kind"
            ),
            **runtime_common,
        }
    )
    _write_artifact(active_policy_path, active_policy)
    rule.update(
        {
            "rule_status": "registered_operator_gated" if runtime_success else rule.get("rule_status"),
            "automatic_recovery_rule_created_in_runtime": runtime_success,
            "recovery_rule_selected_by_runtime_policy": runtime_result.get("selected_rule_ref")
            == f"automatic_recovery_rule:{rule['recovery_rule_id']}",
            **runtime_common,
        }
    )
    _write_artifact(rule_path, rule)
    authority.update(
        {
            "authority_status": "registered_runtime_operator_gated"
            if runtime_success
            else authority.get("authority_status"),
            "dispatch_authority_created_in_runtime": runtime_success,
            "dispatch_authority_registered_runtime": runtime_result.get("dispatch_authority_lookup_status")
            == "found",
            **runtime_common,
        }
    )
    _write_artifact(authority_path, authority)

    return build_policy_authority_summary(artifact_root=root)


def _latest_policy_chain(root: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        "candidate": (_latest_payloads(root, "policy_update_candidate.json") or [("", {})])[0],
        "approval": (_latest_payloads(root, "operator_policy_approval_record.json") or [("", {})])[0],
        "policy": (_latest_payloads(root, "active_policy_version.json") or [("", {})])[0],
        "rule": (_latest_payloads(root, "automatic_recovery_rule.json") or [("", {})])[0],
        "authority": (_latest_payloads(root, "bounded_dispatch_authority.json") or [("", {})])[0],
    }


def build_policy_authority_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Summarize the operator-gated policy authority path."""

    root = Path(artifact_root)
    active_index_path, active_index = _latest_active_index(root)
    chain = _latest_policy_chain(root)
    candidate_path, candidate = chain["candidate"]
    approval_path, approval = chain["approval"]
    policy_path, policy = chain["policy"]
    rule_path, rule = chain["rule"]
    authority_path, authority = chain["authority"]
    candidate, candidate_runtime_blocks = _runtime_claims(candidate)
    approval, approval_runtime_blocks = _runtime_claims(approval)
    policy, policy_runtime_blocks = _runtime_claims(policy)
    rule, rule_runtime_blocks = _runtime_claims(rule)
    authority, authority_runtime_blocks = _runtime_claims(authority)
    active_index_ready = bool(
        active_index.get("index_status") == "updated"
        and _active_index_source_bound(root, active_index)
    )
    refs_consistent = bool(
        active_index_ready
        and candidate.get("source_active_lesson_index_artifact_path") == active_index_path
        and approval.get("policy_update_candidate_artifact_path") == candidate_path
        and policy.get("policy_update_candidate_artifact_path") == candidate_path
        and policy.get("approval_artifact_path") == approval_path
        and rule.get("active_policy_artifact_path") == policy_path
        and authority.get("active_policy_artifact_path") == policy_path
        and authority.get("automatic_recovery_rule_artifact_path") == rule_path
        and authority.get("approval_artifact_path") == approval_path
    )
    rollback_ready = bool(candidate.get("rollback_ref") and policy.get("rollback_ref") == candidate.get("rollback_ref"))
    forbidden_true = [
        key
        for key in (
            "automatic_dispatch_executed",
            "dispatch_executed",
            "physical_execution_invoked",
            "hardware_target_allowed",
            "core_direct_execution_used",
            "llm_gate_judge_used",
            "approval_free_stronger_execution",
            "delivery_completion_claimed",
            "public_sync_performed",
        )
        if candidate.get(key) is True
        or approval.get(key) is True
        or policy.get(key) is True
        or rule.get(key) is True
        or authority.get(key) is True
    ]
    required_true = {
        "policy_update_applied_to_runtime_engine": policy.get("policy_update_applied_in_runtime") is True,
        "recovery_rule_registered_in_runtime_engine": rule.get(
            "automatic_recovery_rule_created_in_runtime"
        )
        is True,
        "dispatch_authority_available_in_runtime_dispatch_table": authority.get(
            "dispatch_authority_created_in_runtime"
        )
        is True,
        "policy_engine_loaded_from_empty_baseline": policy.get(
            "policy_engine_loaded_from_empty_baseline"
        )
        is True,
        "recovery_rule_selected_by_runtime_policy": rule.get(
            "recovery_rule_selected_by_runtime_policy"
        )
        is True,
        "dispatch_authority_registered_runtime": authority.get(
            "dispatch_authority_registered_runtime"
        )
        is True,
        "operator_approval_required": authority.get("operator_approval_required") is True,
        "automatic_dispatch_suppressed": authority.get("automatic_dispatch_suppressed") is True,
    }
    policy_state, policy_state_sha256 = _read_json_sha256(policy.get("policy_engine_state_path"))
    policy_replay, policy_replay_sha256 = _read_json_sha256(policy.get("policy_engine_replay_path"))
    registry_state, registry_state_sha256 = _read_json_sha256(
        rule.get("recovery_rule_registry_state_path")
    )
    authority_state, authority_state_sha256 = _read_json_sha256(
        authority.get("dispatch_authority_table_state_path")
    )
    policy_source_projection_sha256 = _sha256_json(_policy_runtime_source_projection(policy))
    rule_source_projection_sha256 = _sha256_json(_rule_runtime_source_projection(rule))
    authority_source_projection_sha256 = _sha256_json(
        _authority_runtime_source_projection(authority)
    )
    registry_rule = _as_mapping(
        _as_mapping(registry_state.get("rules")).get(str(rule.get("recovery_rule_id") or ""))
    )
    authority_entry = _as_mapping(
        _as_mapping(authority_state.get("authorities")).get(
            str(authority.get("dispatch_authority_id") or "")
        )
    )
    runtime_registry_integrity = {
        "policy_runtime_state_hash_matches": bool(
            policy_state
            and policy_state_sha256 == policy.get("policy_engine_state_sha256")
            and policy_replay
            and policy_replay_sha256 == policy.get("policy_engine_replay_sha256")
        ),
        "policy_runtime_source_projection_matches": bool(
            policy_source_projection_sha256 == policy.get("active_policy_source_projection_sha256")
            and policy_source_projection_sha256
            == policy.get("policy_engine_active_policy_source_projection_sha256")
            and policy_source_projection_sha256
            == policy_state.get("active_policy_source_projection_sha256")
        ),
        "recovery_rule_registry_state_hash_matches": bool(
            registry_state
            and registry_state_sha256 == rule.get("recovery_rule_registry_state_sha256")
        ),
        "recovery_rule_registry_source_projection_matches": bool(
            rule_source_projection_sha256 == rule.get("recovery_rule_source_projection_sha256")
            and rule_source_projection_sha256
            == rule.get("registry_rule_source_projection_sha256")
            and rule_source_projection_sha256
            == registry_rule.get("recovery_rule_source_projection_sha256")
        ),
        "dispatch_authority_table_entry_state_hash_matches": bool(
            authority_entry
            and _sha256_json(authority_entry)
            == (
                authority.get("dispatch_authority_table_entry_state_sha256")
                or authority.get("dispatch_authority_table_state_sha256")
            )
        ),
        "dispatch_authority_table_source_projection_matches": bool(
            authority_source_projection_sha256
            == authority.get("dispatch_authority_source_projection_sha256")
            and authority_source_projection_sha256
            == authority.get("authority_table_source_projection_sha256")
            and authority_source_projection_sha256
            == authority_entry.get("dispatch_authority_source_projection_sha256")
        ),
    }
    required_true.update(runtime_registry_integrity)
    blocking_reasons = []
    if not active_index_ready:
        blocking_reasons.append("active_lesson_index_missing_or_unbound")
    if any(path == "" for path in (candidate_path, approval_path, policy_path, rule_path, authority_path)):
        blocking_reasons.append("policy_authority_chain_incomplete")
    if not refs_consistent:
        blocking_reasons.append("policy_authority_ref_chain_mismatch")
    if not rollback_ready:
        blocking_reasons.append("rollback_ref_missing_or_mismatch")
    missing_required = [key for key, value in required_true.items() if not value]
    blocking_reasons.extend(f"{key}_not_observed" for key in missing_required)
    blocking_reasons.extend(forbidden_true)
    runtime_blocking_reasons = (
        candidate_runtime_blocks
        + approval_runtime_blocks
        + policy_runtime_blocks
        + rule_runtime_blocks
        + authority_runtime_blocks
    )
    blocking_reasons.extend(runtime_blocking_reasons)
    summary_status = "authority_runtime_applied" if not blocking_reasons else "blocked" if any(
        path for path in (candidate_path, approval_path, policy_path, rule_path, authority_path)
    ) else "missing"
    return {
        "schema_version": POLICY_AUTHORITY_SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_status": summary_status,
        "classification": {
            "causal_form": "Form 2a authority runtime preparation",
            "surface": "operator-gated policy-to-recovery authority artifact path",
            "progress_counted": summary_status == "authority_runtime_applied",
            "dispatch_execution_progress_counted": False,
            "closed_loop_runtime_progress_counted": False,
        },
        "active_lesson_index": {
            "status": "updated" if active_index_ready else "missing_or_unbound",
            "artifact_path": active_index_path,
        },
        "policy_update_candidate": {
            "status": candidate.get("candidate_status") or "missing",
            "artifact_path": candidate_path,
            "rollback_ref": candidate.get("rollback_ref"),
        },
        "operator_policy_approval": {
            "status": approval.get("approval_status") or "missing",
            "artifact_path": approval_path,
            "operator_approved": approval.get("operator_approved_in_runtime") is True,
            "operator_approved_in_artifact": approval.get("operator_approved_in_artifact") is True,
            "operator_approved_in_runtime": approval.get("operator_approved_in_runtime") is True,
        },
        "active_policy_version": {
            "status": policy.get("policy_status") or "missing",
            "artifact_path": policy_path,
            "policy_update_applied": policy.get("policy_update_applied_in_runtime") is True,
            "policy_update_recorded_in_artifact": policy.get("policy_update_applied_in_artifact") is True,
            "policy_update_applied_to_runtime_engine": policy.get("policy_update_applied_in_runtime") is True,
            "policy_runtime_state_hash_matches": runtime_registry_integrity[
                "policy_runtime_state_hash_matches"
            ],
            "policy_runtime_source_projection_matches": runtime_registry_integrity[
                "policy_runtime_source_projection_matches"
            ],
        },
        "automatic_recovery_rule": {
            "status": rule.get("rule_status") or "missing",
            "artifact_path": rule_path,
            "automatic_recovery_rule_created": rule.get("automatic_recovery_rule_created_in_runtime") is True,
            "recovery_rule_recorded_in_artifact": rule.get("automatic_recovery_rule_created_in_artifact") is True,
            "recovery_rule_registered_in_runtime_engine": rule.get(
                "automatic_recovery_rule_created_in_runtime"
            )
            is True,
            "registry_state_hash_matches": runtime_registry_integrity[
                "recovery_rule_registry_state_hash_matches"
            ],
            "registry_source_projection_matches": runtime_registry_integrity[
                "recovery_rule_registry_source_projection_matches"
            ],
            "bounded_action_ref": rule.get("bounded_action_ref"),
        },
        "bounded_dispatch_authority": {
            "status": authority.get("authority_status") or "missing",
            "artifact_path": authority_path,
            "dispatch_authority_created": authority.get("dispatch_authority_created_in_runtime") is True,
            "dispatch_authority_recorded_in_artifact": authority.get(
                "dispatch_authority_created_in_artifact"
            )
            is True,
            "dispatch_authority_available_in_runtime_dispatch_table": authority.get(
                "dispatch_authority_created_in_runtime"
            )
            is True,
            "authority_table_state_hash_matches": runtime_registry_integrity[
                "dispatch_authority_table_entry_state_hash_matches"
            ],
            "authority_table_entry_state_hash_matches": runtime_registry_integrity[
                "dispatch_authority_table_entry_state_hash_matches"
            ],
            "authority_table_source_projection_matches": runtime_registry_integrity[
                "dispatch_authority_table_source_projection_matches"
            ],
            "approval_ref": authority.get("approval_ref"),
            "bounded_action_ref": authority.get("bounded_action_ref"),
            "dispatch_ref": authority.get("dispatch_ref"),
        },
        "authority_boundary": {
            "policy_update_applied": policy.get("policy_update_applied_in_runtime") is True,
            "policy_update_recorded_in_artifact": policy.get("policy_update_applied_in_artifact") is True,
            "policy_update_applied_to_runtime_engine": policy.get("policy_update_applied_in_runtime") is True,
            "automatic_recovery_rule_created": rule.get("automatic_recovery_rule_created_in_runtime") is True,
            "recovery_rule_recorded_in_artifact": rule.get("automatic_recovery_rule_created_in_artifact") is True,
            "recovery_rule_registered_in_runtime_engine": rule.get(
                "automatic_recovery_rule_created_in_runtime"
            )
            is True,
            "dispatch_authority_created": authority.get("dispatch_authority_created_in_runtime") is True,
            "dispatch_authority_recorded_in_artifact": authority.get(
                "dispatch_authority_created_in_artifact"
            )
            is True,
            "dispatch_authority_available_in_runtime_dispatch_table": authority.get(
                "dispatch_authority_created_in_runtime"
            )
            is True,
            "operator_approval_required": authority.get("operator_approval_required") is True,
            "automatic_dispatch_suppressed": authority.get("automatic_dispatch_suppressed") is True,
            "dispatch_executed": authority.get("dispatch_executed_in_runtime") is True,
            "dispatch_executed_in_artifact": authority.get("dispatch_executed_in_artifact") is True,
            "dispatch_executed_in_runtime": authority.get("dispatch_executed_in_runtime") is True,
            "automatic_dispatch_executed": authority.get("automatic_dispatch_executed") is True,
            "physical_execution_invoked": authority.get("physical_execution_invoked") is True,
            "hardware_target_allowed": authority.get("hardware_target_allowed") is True,
            "core_direct_execution_used": authority.get("core_direct_execution_used") is True,
            "llm_gate_judge_used": authority.get("llm_gate_judge_used") is True,
            "approval_free_stronger_execution": authority.get("approval_free_stronger_execution") is True,
            "delivery_completion_claimed": authority.get("delivery_completion_claimed") is True,
            "public_sync_performed": authority.get("public_sync_performed") is True,
            "refs_consistent": refs_consistent,
            "rollback_ready": rollback_ready,
            "runtime_registry_integrity": runtime_registry_integrity,
            "runtime_claim_validation": {
                "policy": runtime_claim_validation_summary(policy),
                "rule": runtime_claim_validation_summary(rule),
                "authority": runtime_claim_validation_summary(authority),
            },
            "blocking_reasons": blocking_reasons,
        },
        "operator_note": (
            "This applied the operator-approved policy path to the runtime "
            "policy engine, recovery rule registry, and bounded dispatch "
            "authority table. Dispatch remains operator-gated and has not "
            "executed from this promotion step."
        ),
    }


def _latest_form1_runtime_delta_artifact(
    root: Path,
) -> tuple[str, dict[str, Any], Path | None]:
    search_roots = [
        REPO_ROOT / "artifacts" / "form1_runtime_delta",
        root,
    ]
    hits: list[tuple[int, float, str, Path, dict[str, Any]]] = []
    seen: set[Path] = set()
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for path in search_root.rglob("*.json"):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            payload = _read_json(path)
            if payload is None:
                continue
            form1_payload = _form1_runtime_delta_payload(payload)
            if not form1_payload:
                continue
            source_check = _form1_runtime_delta_source_check(form1_payload)
            source_supported = 1 if source_check.get("source_supported") is True else 0
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            hits.append((source_supported, mtime, path.as_posix(), path, form1_payload))
    if not hits:
        return "", {}, None
    _supported, _mtime, _text, path, payload = sorted(hits, reverse=True)[0]
    return _repo_or_path_relative(path), payload, path


def _form1_runtime_delta_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if payload.get("schema_version") in {
        "drone_behavior_delta_under_wind.v1",
        "drone_behavior_delta_under_payload_mass.v1",
    }:
        return payload
    nested = _as_mapping(payload.get("payload_behavior_delta"))
    if nested.get("schema_version") == "drone_behavior_delta_under_payload_mass.v1":
        return nested
    return {}


def _runtime_evidence_complete(evidence: Mapping[str, Any]) -> bool:
    return bool(
        evidence.get("schema_version") == "runtime_invocation_evidence.v1"
        and evidence.get("invocation_kind") == "subprocess"
        and evidence.get("invocation_target")
        and isinstance(evidence.get("process_pid"), int)
        and not isinstance(evidence.get("process_pid"), bool)
        and evidence.get("invocation_exit_code") == 0
        and isinstance(evidence.get("invocation_stdout_sha256"), str)
        and len(str(evidence.get("invocation_stdout_sha256"))) == 64
        and isinstance(evidence.get("invocation_stderr_sha256"), str)
        and len(str(evidence.get("invocation_stderr_sha256"))) == 64
        and evidence.get("runtime_summary_path")
    )


def _form1_runtime_delta_source_check(form1: Mapping[str, Any]) -> dict[str, Any]:
    if form1.get("schema_version") == "drone_behavior_delta_under_payload_mass.v1":
        return _payload_form1_runtime_delta_source_check(form1)

    metrics = _as_mapping(form1.get("metrics"))
    source_binding = _as_mapping(form1.get("source_binding"))
    runtime_pairing = _as_mapping(form1.get("runtime_pairing"))
    baseline_evidence = _as_mapping(form1.get("baseline_runtime_invocation_evidence"))
    condition_evidence = _as_mapping(form1.get("condition_runtime_invocation_evidence"))
    requested = _as_mapping(form1.get("requested"))
    reasons: list[str] = []

    checks = {
        "schema_version_supported": form1.get("schema_version")
        == "drone_behavior_delta_under_wind.v1",
        "causal_form_supported": form1.get("causal_form") in {"Form 1a", "Form 1b"},
        "progress_counted": form1.get("progress_counted") is True,
        "drone_physics_affected": form1.get("drone_physics_affected") is True,
        "form1_scope_supported": form1.get("form1_scope")
        == "drone_physics_or_mission_behavior",
        "runtime_invocation_evidence_complete": (
            source_binding.get("runtime_invocation_evidence_complete") is True
            and _runtime_evidence_complete(baseline_evidence)
            and _runtime_evidence_complete(condition_evidence)
        ),
        "runtime_pairing_complete": source_binding.get("runtime_pairing_complete") is True,
        "command_argv_sha256_equal": runtime_pairing.get("command_argv_sha256_equal") is True,
        "condition_only_env_delta": runtime_pairing.get("condition_only_env_delta") is True,
        "source_boundary_flags_safe": source_binding.get("source_boundary_flags_safe") is True,
        "raw_trajectory_delta_above_threshold": form1.get("raw_trajectory_delta_above_threshold")
        is True,
        "max_delta_observed": float(metrics.get("max_observed_delta_m") or 0.0) > 0.0,
        "threshold_positive": float(metrics.get("delta_threshold_m") or 0.0) > 0.0,
        "observed_wind_delta_present": (
            requested.get("observed_wind_a_mps") is not None
            and requested.get("observed_wind_b_mps") is not None
            and requested.get("observed_wind_a_mps") != requested.get("observed_wind_b_mps")
        ),
    }
    for key, value in checks.items():
        if not value:
            reasons.append(f"{key}_not_observed")
    return {
        "schema_version": "missionos_form1_source_check.v1",
        "source_supported": not reasons,
        "checks": checks,
        "unsupported_reasons": reasons,
        "input_causal_form": form1.get("causal_form"),
        "input_progress_counted": form1.get("progress_counted") is True,
        "input_drone_physics_affected": form1.get("drone_physics_affected") is True,
        "max_observed_delta_m": metrics.get("max_observed_delta_m"),
        "delta_threshold_m": metrics.get("delta_threshold_m"),
        "margin_ratio": form1.get("observed_delta_margin_ratio"),
        "observed_wind_a_mps": requested.get("observed_wind_a_mps"),
        "observed_wind_b_mps": requested.get("observed_wind_b_mps"),
        "baseline_invocation_id": baseline_evidence.get("invocation_id"),
        "condition_invocation_id": condition_evidence.get("invocation_id"),
    }


def _payload_form1_runtime_delta_source_check(form1: Mapping[str, Any]) -> dict[str, Any]:
    metrics = _as_mapping(form1.get("metrics"))
    requested = _as_mapping(form1.get("requested"))
    source_binding = _as_mapping(form1.get("source_binding"))
    reasons: list[str] = []
    checks = {
        "schema_version_supported": form1.get("schema_version")
        == "drone_behavior_delta_under_payload_mass.v1",
        "form1_claim_supported": form1.get("form1_claim_supported") is True,
        "payload_behavior_delta_observed": form1.get("payload_behavior_delta_observed")
        is True,
        "raw_trajectory_delta_above_threshold": form1.get("raw_behavior_delta_above_threshold")
        is True,
        "source_boundary_flags_safe": source_binding.get("source_boundary_flags_safe")
        is True,
        "source_runs_interpretable": source_binding.get("source_runs_interpretable")
        is True,
        "route_geometry_match": source_binding.get("route_geometry_match") is True,
        "max_delta_observed": float(metrics.get("max_observed_delta_m") or 0.0) > 0.0,
        "threshold_positive": float(metrics.get("delta_threshold_m") or 0.0) > 0.0,
        "payload_delta_present": (
            requested.get("light_payload_kg") is not None
            and requested.get("heavy_payload_kg") is not None
            and requested.get("light_payload_kg") != requested.get("heavy_payload_kg")
        ),
    }
    for key, value in checks.items():
        if not value:
            reasons.append(f"{key}_not_observed")
    threshold = float(metrics.get("climb_time_delta_threshold_seconds") or 0.0)
    climb_delta = metrics.get("climb_elapsed_seconds_delta_at_target_z")
    margin = None
    try:
        if threshold > 0.0:
            margin = float(climb_delta) / threshold
    except (TypeError, ValueError):
        margin = None
    return {
        "schema_version": "missionos_form1_source_check.v1",
        "source_supported": not reasons,
        "checks": checks,
        "unsupported_reasons": reasons,
        "input_causal_form": form1.get("causal_form") or "Form 1a",
        "input_progress_counted": form1.get("form1_claim_supported") is True,
        "input_drone_physics_affected": form1.get("drone_behavior_affected") is True,
        "condition_kind": form1.get("condition_kind"),
        "max_observed_delta_m": metrics.get("max_observed_delta_m"),
        "delta_threshold_m": metrics.get("delta_threshold_m"),
        "margin_ratio": margin,
        "light_payload_kg": requested.get("light_payload_kg"),
        "heavy_payload_kg": requested.get("heavy_payload_kg"),
    }


def _select_form2a_wind_response(form1: Mapping[str, Any]) -> dict[str, Any]:
    if form1.get("schema_version") == "drone_behavior_delta_under_payload_mass.v1":
        metrics = _as_mapping(form1.get("metrics"))
        requested = _as_mapping(form1.get("requested"))
        threshold = float(metrics.get("climb_time_delta_threshold_seconds") or 0.0)
        climb_delta = metrics.get("climb_elapsed_seconds_delta_at_target_z")
        try:
            margin = float(climb_delta) / threshold if threshold > 0.0 else 0.0
        except (TypeError, ValueError):
            margin = 0.0
        return {
            "intelligence_source": INTERIM_RULE_INTELLIGENCE_SOURCE,
            "eligible_for_ai_agent_progress": False,
            "llm_judgment_in_gate": False,
            "selected_response_kind": FORM2A_PAYLOAD_RECOVERY_RESPONSE_KIND,
            "bounded_action_kind": FORM2A_PAYLOAD_RECOVERY_RESPONSE_KIND,
            "selection_reason": "form1_payload_climb_delay_operator_recovery_required",
            "response_urgency": "operator_review_required",
            "mission_response_kind": "action",
            "trigger_level": "level_1_direct",
            "source_condition_kind": form1.get("condition_kind"),
            "source_margin_ratio": margin,
            "source_max_observed_delta_m": metrics.get("max_observed_delta_m"),
            "source_delta_threshold_m": metrics.get("delta_threshold_m"),
            "source_observed_wind_a_mps": None,
            "source_observed_wind_b_mps": None,
            "source_light_payload_kg": requested.get("light_payload_kg"),
            "source_heavy_payload_kg": requested.get("heavy_payload_kg"),
        }
    metrics = _as_mapping(form1.get("metrics"))
    requested = _as_mapping(form1.get("requested"))
    margin = float(form1.get("observed_delta_margin_ratio") or 0.0)
    max_delta = float(metrics.get("max_observed_delta_m") or 0.0)
    threshold = float(metrics.get("delta_threshold_m") or 0.0)
    if margin >= 5.0:
        action_kind = "operator_gated_wind_replan_with_compensation"
        reason = "form1a_wind_delta_margin_above_5x"
        urgency = "high"
    elif margin >= 2.0:
        action_kind = "operator_gated_wind_compensated_reroute"
        reason = "form1a_wind_delta_margin_above_2x"
        urgency = "medium"
    else:
        action_kind = "operator_gated_continue_with_wind_warning"
        reason = "form1b_wind_delta_above_threshold"
        urgency = "low"
    return {
        "intelligence_source": INTERIM_RULE_INTELLIGENCE_SOURCE,
        "eligible_for_ai_agent_progress": False,
        "llm_judgment_in_gate": False,
        "selected_response_kind": action_kind,
        "bounded_action_kind": action_kind,
        "selection_reason": reason,
        "response_urgency": urgency,
        "mission_response_kind": "action",
        "trigger_level": "level_1_direct",
        "source_condition_kind": form1.get("condition_kind"),
        "source_margin_ratio": margin,
        "source_max_observed_delta_m": max_delta,
        "source_delta_threshold_m": threshold,
        "source_observed_wind_a_mps": requested.get("observed_wind_a_mps"),
        "source_observed_wind_b_mps": requested.get("observed_wind_b_mps"),
    }


def _select_form2a_wind_response_from_llm_proposal(
    *,
    form1: Mapping[str, Any],
    planner_result: Mapping[str, Any],
) -> dict[str, Any]:
    metrics = _as_mapping(form1.get("metrics"))
    requested = _as_mapping(form1.get("requested"))
    source_check = _form1_runtime_delta_source_check(form1)
    proposal = _as_mapping(planner_result.get("proposal"))
    parameters = _as_mapping(proposal.get("parameters"))
    response_kind = str(proposal.get("response_kind") or "")
    return {
        "intelligence_source": AI_AGENT_PROGRESS_ELIGIBLE_INTELLIGENCE_SOURCE,
        "eligible_for_ai_agent_progress": True,
        "llm_judgment_in_gate": False,
        "selected_response_kind": response_kind,
        "bounded_action_kind": response_kind,
        "selection_reason": "llm_response_planner_proposal_guardrail_passed",
        "response_urgency": str(
            parameters.get("urgency") or "operator_review_required"
        ),
        "mission_response_kind": "action",
        "trigger_level": "level_1_direct",
        "source_condition_kind": form1.get("condition_kind"),
        "source_margin_ratio": float(
            form1.get("observed_delta_margin_ratio")
            or source_check.get("margin_ratio")
            or 0.0
        ),
        "source_max_observed_delta_m": float(metrics.get("max_observed_delta_m") or 0.0),
        "source_delta_threshold_m": float(metrics.get("delta_threshold_m") or 0.0),
        "source_observed_wind_a_mps": requested.get("observed_wind_a_mps"),
        "source_observed_wind_b_mps": requested.get("observed_wind_b_mps"),
        "source_light_payload_kg": requested.get("light_payload_kg"),
        "source_heavy_payload_kg": requested.get("heavy_payload_kg"),
        "llm_response_proposal_ref": planner_result.get("proposal_ref") or "",
        "llm_response_proposal_artifact_path": planner_result.get(
            "proposal_artifact_path"
        )
        or "",
        "llm_response_planner_status": planner_result.get("planner_status") or "",
        "llm_response_planner_guardrail": dict(
            _as_mapping(planner_result.get("guardrail"))
        ),
        "llm_response_parameters": dict(parameters),
        "llm_response_rationale": proposal.get("rationale") or "",
        "llm_response_expected_outcome": proposal.get("expected_outcome") or "",
        "llm_response_uncertainty": proposal.get("uncertainty") or "",
        "llm_response_approval_request": proposal.get("approval_request") or "",
    }


def _form2a_llm_response_kind_compatible_with_source(
    *,
    form1: Mapping[str, Any],
    response_kind: str,
) -> bool:
    condition_kind = str(form1.get("condition_kind") or "")
    if form1.get("schema_version") == "drone_behavior_delta_under_payload_mass.v1":
        return response_kind == FORM2A_PAYLOAD_RECOVERY_RESPONSE_KIND
    if condition_kind == "payload_mass_drone_behavior_delta":
        return response_kind == FORM2A_PAYLOAD_RECOVERY_RESPONSE_KIND
    if "wind" in condition_kind:
        return response_kind in {
            *FORM2A_COMPENSATION_RESPONSE_KINDS,
            FORM2A_WARNING_RESPONSE_KIND,
        }
    return True


def _form2a_response_selection_planner_fallback_source(
    planner_result: Mapping[str, Any],
) -> str:
    _ = planner_result
    return GUARDRAIL_FALLBACK_INTELLIGENCE_SOURCE


def _latest_form2a_response_selection_chain(root: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        "selection": (_latest_payloads(root, "missionos_form2a_response_selection.json") or [("", {})])[0],
        "token": (
            _latest_payloads(root, "missionos_form2a_operator_approval_token.json")
            or [("", {})]
        )[0],
        "human_review": (
            _latest_payloads(root, "missionos_form2a_human_operator_review.json")
            or [("", {})]
        )[0],
    }


def run_form2a_response_selection_from_form1(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    form1_artifact_path: Path | str | None = None,
    operator_instruction: str | Mapping[str, Any] | None = None,
    capability_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Select a bounded Form 2a response from the source-bound Form 1 wind delta.

    This issues an operator-approval token artifact and planned refs only. It
    deliberately does not consume the token or execute dispatch.
    """

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    if form1_artifact_path is None:
        source_artifact_path, form1, source_path = _latest_form1_runtime_delta_artifact(root)
    else:
        source_path = _resolve_repo_or_artifact_path(root, form1_artifact_path)
        source_raw = _read_json(source_path) or {}
        form1 = dict(_form1_runtime_delta_payload(source_raw))
        source_artifact_path = _repo_or_path_relative(source_path)
    if not form1 or source_path is None:
        return build_form2a_response_selection_summary(artifact_root=root)

    generated_at_dt = datetime.now(timezone.utc)
    generated_at = generated_at_dt.isoformat()
    operator_instruction_payload: dict[str, Any] = {}
    if isinstance(operator_instruction, Mapping):
        instruction_text = str(
            operator_instruction.get("text")
            or operator_instruction.get("instruction")
            or ""
        ).strip()
        instruction_source = str(
            operator_instruction.get("source") or "missionos_autonomy_monitor"
        )
    else:
        instruction_text = str(operator_instruction or "").strip()
        instruction_source = "missionos_autonomy_monitor"
    if instruction_text:
        operator_instruction_payload = {
            "text": instruction_text[:2000],
            "source": instruction_source,
            "received_at": generated_at,
        }
    source_check = _form1_runtime_delta_source_check(form1)
    selection_dir = _artifact_dir(root, "missionos_form2a_response_selection")
    token_dir = _artifact_dir(root, "missionos_form2a_operator_approval_token")
    selection_path = selection_dir / "missionos_form2a_response_selection.json"
    token_path = token_dir / "missionos_form2a_operator_approval_token.json"
    response_selection_id = f"missionos_form2a_response_selection_{uuid.uuid4().hex[:12]}"
    approval_token_id = f"missionos_form2a_operator_approval_token_{uuid.uuid4().hex[:12]}"
    approval_ref = f"missionos_form2a_operator_approval_token:{approval_token_id}"
    bounded_action_ref = f"missionos_bounded_action:{uuid.uuid4().hex[:12]}"
    dispatch_ref = f"missionos_pending_dispatch:{uuid.uuid4().hex[:12]}"
    input_sha256 = _sha256_file(source_path)
    capability_context_payload = dict(
        _as_mapping(capability_context)
        or capability_invocation_context(
            "form2a_response_selection",
            requested_by="direct_gateway_route",
            source_route="/missionos/form2a-response-selection/run",
            request_payload={
                "operator_instruction": operator_instruction_payload,
                "input_form1_artifact_path": source_artifact_path,
                "input_form1_artifact_sha256": input_sha256,
            },
        )
    )
    selection_dir.mkdir(parents=True, exist_ok=True)
    token_dir.mkdir(parents=True, exist_ok=True)

    if source_check["source_supported"] is not True:
        blocked = {
            "schema_version": FORM2A_RESPONSE_SELECTION_SCHEMA_VERSION,
            "response_selection_id": response_selection_id,
            "selection_status": "blocked",
            "generated_at": generated_at,
            "causal_form": "Form 0b",
            "form2_subtype": "Form 2a candidate",
            "progress_counted": False,
            "form2a_response_selection_progress_counted_in_artifact": False,
            "form2a_response_selection_progress_counted_in_runtime": False,
            "goal_640_progress_counted": False,
            "form2a_response_selected_in_artifact": False,
            "form2a_response_selected_in_runtime": False,
            "input_form1_artifact_path": source_artifact_path,
            "input_form1_artifact_sha256": input_sha256,
            "source_check": source_check,
            "operator_instruction": operator_instruction_payload,
            "capability_invocation": capability_context_payload,
            "capability_invocation_ref": capability_context_payload.get(
                "capability_invocation_ref", ""
            ),
            "operator_facing_route": capability_context_payload.get(
                "operator_facing_route", ""
            ),
            "requested_by": capability_context_payload.get("requested_by", ""),
            "blocking_reasons": list(source_check["unsupported_reasons"]),
            "operator_approval_required": True,
            "operator_approval_token_issued_in_artifact": False,
            "operator_approval_token_consumed_in_runtime": False,
            "dispatch_executed_in_runtime": False,
            "automatic_dispatch_executed": False,
            "physical_execution_invoked": False,
            "hardware_target_allowed": False,
            "core_direct_execution_used": False,
            "llm_gate_judge_used": False,
            "approval_free_stronger_execution": False,
            "delivery_completion_claimed": False,
            "public_sync_performed": False,
            "drone_physics_affected": False,
            "intelligence_source": INTERIM_RULE_INTELLIGENCE_SOURCE,
            "eligible_for_ai_agent_progress": False,
            "ai_agent_progress_counted": False,
            "llm_judgment_in_gate": False,
        }
        _write_artifact(selection_path, blocked)
        return build_form2a_response_selection_summary(artifact_root=root)

    planner_result = run_llm_response_planner(
        form1_artifact=form1,
        source_check=source_check,
        artifact_root=root,
        artifact_relative=_relative,
        operator_instruction=operator_instruction_payload,
    )
    if planner_result.get("planner_status") == "proposal_guardrail_passed":
        response = _select_form2a_wind_response_from_llm_proposal(
            form1=form1,
            planner_result=planner_result,
        )
        if not _form2a_llm_response_kind_compatible_with_source(
            form1=form1,
            response_kind=str(response.get("selected_response_kind") or ""),
        ):
            response = _select_form2a_wind_response(form1)
            response["intelligence_source"] = GUARDRAIL_FALLBACK_INTELLIGENCE_SOURCE
            response["eligible_for_ai_agent_progress"] = False
            response["llm_response_planner_status"] = "guardrail_blocked"
            response["llm_response_planner_blocking_reasons"] = [
                "response_kind_not_compatible_with_source_condition_kind"
            ]
            response["llm_response_planner_guardrail"] = dict(
                _as_mapping(planner_result.get("guardrail"))
            )
            response["llm_response_proposal_ref"] = (
                planner_result.get("proposal_ref") or ""
            )
            response["llm_response_proposal_artifact_path"] = (
                planner_result.get("proposal_artifact_path") or ""
            )
    else:
        response = _select_form2a_wind_response(form1)
        response["intelligence_source"] = _form2a_response_selection_planner_fallback_source(
            planner_result
        )
        response["eligible_for_ai_agent_progress"] = False
        response["llm_response_planner_status"] = planner_result.get("planner_status")
        response["llm_response_planner_blocking_reasons"] = list(
            planner_result.get("blocking_reasons") or []
        )
        response["llm_response_planner_guardrail"] = dict(
            _as_mapping(planner_result.get("guardrail"))
        )
        response["llm_response_proposal_ref"] = planner_result.get("proposal_ref") or ""
        response["llm_response_proposal_artifact_path"] = (
            planner_result.get("proposal_artifact_path") or ""
        )
    token_expires_at = (generated_at_dt + timedelta(minutes=30)).isoformat()
    approval_request_tool = approval_request_tool_record(
        approval_scope="form2a_bounded_action_operator_review",
        approval_payload={
            "response_selection_id": response_selection_id,
            "selected_response_kind": response["selected_response_kind"],
            "bounded_action_ref": bounded_action_ref,
            "dispatch_ref": dispatch_ref,
            "input_form1_artifact_path": source_artifact_path,
            "input_form1_artifact_sha256": input_sha256,
        },
        capability_context=capability_context_payload,
        approval_ref=approval_ref,
        approval_artifact_path=_relative(token_path),
        expires_at=token_expires_at,
    )
    approval_request_artifact_path = _write_approval_request_tool_artifact(
        root=root,
        approval_request_tool=approval_request_tool,
    )
    approval_request_tool = dict(approval_request_tool)
    approval_request_tool["approval_request_artifact_path"] = (
        approval_request_artifact_path
    )
    selection = {
        "schema_version": FORM2A_RESPONSE_SELECTION_SCHEMA_VERSION,
        "response_selection_id": response_selection_id,
        "selection_status": "selected",
        "generated_at": generated_at,
        "causal_form": "Form 2a",
        "form2_subtype": "Form 2a",
        "progress_counted": False,
        "form2a_response_selection_progress_counted_in_artifact": True,
        "form2a_response_selection_progress_counted_in_runtime": False,
        "goal_640_progress_counted": False,
        "form2a_response_selected_in_artifact": True,
        "form2a_response_selected_in_runtime": False,
        "input_form1_artifact_path": source_artifact_path,
        "input_form1_artifact_sha256": input_sha256,
        "input_form1_ref": f"{form1.get('schema_version')}:{form1.get('audit_id')}",
        "input_causal_form": form1.get("causal_form") or "Form 1a",
        "input_form1_scope": form1.get("form1_scope")
        or "drone_physics_or_mission_behavior",
        "source_check": source_check,
        "operator_instruction": operator_instruction_payload,
        "capability_invocation": capability_context_payload,
        "capability_invocation_ref": capability_context_payload.get(
            "capability_invocation_ref", ""
        ),
        "operator_facing_route": capability_context_payload.get(
            "operator_facing_route", ""
        ),
        "requested_by": capability_context_payload.get("requested_by", ""),
        "chief_agent_invocation_ref": capability_context_payload.get(
            "chief_agent_invocation_ref", ""
        ),
        "specialist_agent_invocation_ref": capability_context_payload.get(
            "specialist_agent_invocation_ref", ""
        ),
        "safety_critic_ref": capability_context_payload.get("safety_critic_ref", ""),
        "intelligence_source": response["intelligence_source"],
        "eligible_for_ai_agent_progress": response["eligible_for_ai_agent_progress"],
        "ai_agent_progress_counted": False,
        "llm_judgment_in_gate": response["llm_judgment_in_gate"],
        "mission_response_kind": response["mission_response_kind"],
        "selected_response_kind": response["selected_response_kind"],
        "bounded_action_kind": response["bounded_action_kind"],
        "selection_reason": response["selection_reason"],
        "response_urgency": response["response_urgency"],
        "trigger_level": response["trigger_level"],
        "source_condition_kind": response["source_condition_kind"],
        "source_margin_ratio": response["source_margin_ratio"],
        "source_max_observed_delta_m": response["source_max_observed_delta_m"],
        "source_delta_threshold_m": response["source_delta_threshold_m"],
        "source_observed_wind_a_mps": response["source_observed_wind_a_mps"],
        "source_observed_wind_b_mps": response["source_observed_wind_b_mps"],
        "source_light_payload_kg": response.get("source_light_payload_kg"),
        "source_heavy_payload_kg": response.get("source_heavy_payload_kg"),
        "llm_response_planner_status": response.get("llm_response_planner_status")
        or "not_configured",
        "llm_response_planner_blocking_reasons": list(
            response.get("llm_response_planner_blocking_reasons") or []
        ),
        "llm_response_planner_guardrail": dict(
            _as_mapping(response.get("llm_response_planner_guardrail"))
        ),
        "llm_response_proposal_ref": response.get("llm_response_proposal_ref") or "",
        "llm_response_proposal_artifact_path": response.get(
            "llm_response_proposal_artifact_path"
        )
        or "",
        "llm_response_parameters": dict(
            _as_mapping(response.get("llm_response_parameters"))
        ),
        "llm_response_rationale": response.get("llm_response_rationale") or "",
        "llm_response_expected_outcome": response.get("llm_response_expected_outcome")
        or "",
        "llm_response_uncertainty": response.get("llm_response_uncertainty") or "",
        "llm_response_approval_request": response.get("llm_response_approval_request")
        or "",
        "approval_ref": approval_ref,
        "approval_artifact_path": _relative(token_path),
        "approval_request_tool": approval_request_tool,
        "approval_request_ref": approval_request_tool["approval_request_ref"],
        "approval_request_artifact_path": approval_request_artifact_path,
        "operator_approval_required": True,
        "operator_approval_token_issued_in_artifact": True,
        "operator_approval_token_consumed_in_runtime": False,
        "bounded_action_ref": bounded_action_ref,
        "dispatch_ref": dispatch_ref,
        "dispatch_execution_status": "not_executed",
        "dispatch_executed_in_runtime": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "core_direct_execution_used": False,
        "llm_gate_judge_used": False,
        "approval_free_stronger_execution": False,
        "delivery_completion_claimed": False,
        "public_sync_performed": False,
        "drone_physics_affected": False,
        "next_required_applicator": "operator_approved_dispatch_execution_token_consume",
    }
    selection = _write_artifact(selection_path, selection)
    token = {
        "schema_version": FORM2A_OPERATOR_APPROVAL_TOKEN_SCHEMA_VERSION,
        "approval_token_id": approval_token_id,
        "approval_token_status": "issued_unconsumed",
        "generated_at": generated_at,
        "expires_at": token_expires_at,
        "token_ttl_seconds": 1800,
        "causal_form": "Form 2a approval token",
        "progress_counted": False,
        "response_selection_ref": f"missionos_form2a_response_selection:{response_selection_id}",
        "response_selection_artifact_path": _relative(selection_path),
        "input_form1_artifact_path": source_artifact_path,
        "input_form1_artifact_sha256": input_sha256,
        "operator_instruction": operator_instruction_payload,
        "capability_invocation": capability_context_payload,
        "capability_invocation_ref": capability_context_payload.get(
            "capability_invocation_ref", ""
        ),
        "operator_facing_route": capability_context_payload.get(
            "operator_facing_route", ""
        ),
        "requested_by": capability_context_payload.get("requested_by", ""),
        "approval_request_tool": approval_request_tool,
        "approval_request_ref": approval_request_tool["approval_request_ref"],
        "approval_request_artifact_path": approval_request_artifact_path,
        "operator_approval_required": True,
        "operator_approval_token_issued_in_artifact": True,
        "operator_approval_token_consumed_in_runtime": False,
        "approval_ref": approval_ref,
        "bounded_action_ref": bounded_action_ref,
        "dispatch_ref": dispatch_ref,
        "mission_response_kind": response["mission_response_kind"],
        "selected_response_kind": response["selected_response_kind"],
        "llm_response_planner_status": response.get("llm_response_planner_status")
        or "not_configured",
        "llm_response_proposal_ref": response.get("llm_response_proposal_ref") or "",
        "llm_response_proposal_artifact_path": response.get(
            "llm_response_proposal_artifact_path"
        )
        or "",
        "intelligence_source": response["intelligence_source"],
        "eligible_for_ai_agent_progress": response["eligible_for_ai_agent_progress"],
        "ai_agent_progress_counted": False,
        "llm_judgment_in_gate": response["llm_judgment_in_gate"],
        "automatic_dispatch_executed": False,
        "dispatch_executed_in_runtime": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "core_direct_execution_used": False,
        "llm_gate_judge_used": False,
        "approval_free_stronger_execution": False,
        "delivery_completion_claimed": False,
        "public_sync_performed": False,
        "drone_physics_affected": False,
        "token_consumption_scope": "missionos_dispatch_runtime_only",
    }
    _write_artifact(token_path, token)
    return build_form2a_response_selection_summary(artifact_root=root)


def build_form2a_response_selection_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Summarize Form 2a response selection without dispatch execution."""

    root = Path(artifact_root)
    chain = _latest_form2a_response_selection_chain(root)
    selection_path, selection_raw = chain["selection"]
    token_path, token_raw = chain["token"]
    selection, selection_runtime_blocks = _runtime_claims(selection_raw)
    token, token_runtime_blocks = _runtime_claims(token_raw)
    source_path = (
        _resolve_repo_or_artifact_path(root, str(selection.get("input_form1_artifact_path") or ""))
        if selection.get("input_form1_artifact_path")
        else None
    )
    source_hash_matches = bool(
        source_path
        and source_path.exists()
        and selection.get("input_form1_artifact_sha256") == _sha256_file(source_path)
    )
    token_refs_consistent = bool(
        token_path
        and selection.get("approval_artifact_path") == token_path
        and token.get("response_selection_artifact_path") == selection_path
        and token.get("response_selection_ref")
        == f"missionos_form2a_response_selection:{selection.get('response_selection_id')}"
        and token.get("approval_ref") == selection.get("approval_ref")
        and token.get("bounded_action_ref") == selection.get("bounded_action_ref")
        and token.get("dispatch_ref") == selection.get("dispatch_ref")
    )
    source_check = _as_mapping(selection.get("source_check"))
    forbidden_true = [
        key
        for key in (
            "automatic_dispatch_executed",
            "dispatch_executed",
            "physical_execution_invoked",
            "hardware_target_allowed",
            "core_direct_execution_used",
            "llm_gate_judge_used",
            "approval_free_stronger_execution",
            "delivery_completion_claimed",
            "public_sync_performed",
        )
        if selection.get(key) is True or token.get(key) is True
    ]
    required_true = {
        "source_form1_supported": source_check.get("source_supported") is True,
        "source_form1_hash_matches": source_hash_matches,
        "selection_status_selected": selection.get("selection_status") == "selected",
        "form2a_response_selected_in_artifact": selection.get(
            "form2a_response_selected_in_artifact"
        )
        is True,
        "mission_response_kind_action": selection.get("mission_response_kind") == "action",
        "operator_approval_required": selection.get("operator_approval_required") is True,
        "operator_approval_token_issued_in_artifact": selection.get(
            "operator_approval_token_issued_in_artifact"
        )
        is True,
        "operator_approval_token_unconsumed": token.get(
            "operator_approval_token_consumed_in_runtime"
        )
        is False,
        "bounded_action_ref_present": bool(selection.get("bounded_action_ref")),
        "dispatch_ref_present": bool(selection.get("dispatch_ref")),
        "token_refs_consistent": token_refs_consistent,
    }
    blocking_reasons = []
    if not selection_path:
        blocking_reasons.append("form2a_response_selection_missing")
    if selection_path and not token_path:
        blocking_reasons.append("form2a_operator_approval_token_missing")
    blocking_reasons.extend(
        f"{key}_not_observed" for key, value in required_true.items() if not value
    )
    blocking_reasons.extend(forbidden_true)
    blocking_reasons.extend(selection_runtime_blocks + token_runtime_blocks)
    summary_status = (
        "form2a_response_selected"
        if not blocking_reasons
        else "blocked"
        if selection_path
        else "missing"
    )
    return {
        "schema_version": FORM2A_RESPONSE_SELECTION_SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_status": summary_status,
        "classification": {
            "causal_form": "Form 2a" if summary_status == "form2a_response_selected" else "Form 0b",
            "surface": "Form 1 runtime behavior delta to operator-gated Form 2a response selection",
            "progress_counted": False,
            "form2a_response_selection_progress_counted_in_artifact": (
                summary_status == "form2a_response_selected"
            ),
            "form2a_response_selection_progress_counted_in_runtime": False,
            "dispatch_execution_progress_counted": False,
            "closed_loop_runtime_progress_counted": False,
            "drone_physics_affected": False,
            "goal_640_progress_counted": False,
            "ai_agent_progress_counted": False,
        },
        "source_form1": {
            "status": "supported" if source_check.get("source_supported") is True else "blocked",
            "artifact_path": selection.get("input_form1_artifact_path") or "",
            "artifact_sha256": selection.get("input_form1_artifact_sha256") or "",
            "artifact_sha256_matches_current_file": source_hash_matches,
            "causal_form": selection.get("input_causal_form"),
            "form1_scope": selection.get("input_form1_scope"),
            "max_observed_delta_m": selection.get("source_max_observed_delta_m"),
            "delta_threshold_m": selection.get("source_delta_threshold_m"),
            "margin_ratio": selection.get("source_margin_ratio"),
            "observed_wind_a_mps": selection.get("source_observed_wind_a_mps"),
            "observed_wind_b_mps": selection.get("source_observed_wind_b_mps"),
            "light_payload_kg": selection.get("source_light_payload_kg"),
            "heavy_payload_kg": selection.get("source_heavy_payload_kg"),
        },
        "response_selection": {
            "status": selection.get("selection_status") or "missing",
            "artifact_path": selection_path,
            "response_selection_id": selection.get("response_selection_id"),
            "form2a_response_selected_in_artifact": selection.get(
                "form2a_response_selected_in_artifact"
            )
            is True,
            "form2a_response_selected_in_runtime": selection.get(
                "form2a_response_selected_in_runtime"
            )
            is True,
            "form2a_response_selection_progress_counted_in_artifact": selection.get(
                "form2a_response_selection_progress_counted_in_artifact"
            )
            is True,
            "form2a_response_selection_progress_counted_in_runtime": selection.get(
                "form2a_response_selection_progress_counted_in_runtime"
            )
            is True,
            "goal_640_progress_counted": selection.get("goal_640_progress_counted") is True,
            "intelligence_source": selection.get("intelligence_source"),
            "eligible_for_ai_agent_progress": selection.get(
                "eligible_for_ai_agent_progress"
            )
            is True,
            "ai_agent_progress_counted": selection.get("ai_agent_progress_counted")
            is True,
            "llm_judgment_in_gate": selection.get("llm_judgment_in_gate") is True,
            "mission_response_kind": selection.get("mission_response_kind"),
            "selected_response_kind": selection.get("selected_response_kind"),
            "bounded_action_kind": selection.get("bounded_action_kind"),
            "selection_reason": selection.get("selection_reason"),
            "trigger_level": selection.get("trigger_level"),
            "llm_response_planner_status": selection.get("llm_response_planner_status")
            or "not_configured",
            "llm_response_planner_blocking_reasons": list(
                selection.get("llm_response_planner_blocking_reasons") or []
            ),
            "llm_response_proposal_ref": selection.get("llm_response_proposal_ref")
            or "",
            "llm_response_proposal_artifact_path": selection.get(
                "llm_response_proposal_artifact_path"
            )
            or "",
            "llm_response_parameters": dict(
                _as_mapping(selection.get("llm_response_parameters"))
            ),
            "llm_response_rationale": selection.get("llm_response_rationale") or "",
            "llm_response_expected_outcome": selection.get(
                "llm_response_expected_outcome"
            )
            or "",
            "llm_response_uncertainty": selection.get("llm_response_uncertainty")
            or "",
            "llm_response_approval_request": selection.get(
                "llm_response_approval_request"
            )
            or "",
            "operator_instruction": dict(
                _as_mapping(selection.get("operator_instruction"))
            ),
            "capability_invocation_ref": selection.get("capability_invocation_ref") or "",
            "capability_id": _as_mapping(
                selection.get("capability_invocation")
            ).get("capability_id")
            or "",
            "operator_facing_route": selection.get("operator_facing_route") or "",
            "requested_by": selection.get("requested_by") or "",
            "approval_request_ref": selection.get("approval_request_ref") or "",
            "approval_request_artifact_path": selection.get(
                "approval_request_artifact_path", ""
            ),
            "approval_request_tool": dict(
                _as_mapping(selection.get("approval_request_tool"))
            ),
            "approval_ref": selection.get("approval_ref"),
            "bounded_action_ref": selection.get("bounded_action_ref"),
            "dispatch_ref": selection.get("dispatch_ref"),
        },
        "operator_approval_token": {
            "status": token.get("approval_token_status") or "missing",
            "artifact_path": token_path,
            "approval_token_id": token.get("approval_token_id"),
            "operator_approval_token_issued_in_artifact": token.get(
                "operator_approval_token_issued_in_artifact"
            )
            is True,
            "operator_approval_token_consumed_in_runtime": token.get(
                "operator_approval_token_consumed_in_runtime"
            )
            is True,
            "expires_at": token.get("expires_at"),
            "token_ttl_seconds": token.get("token_ttl_seconds"),
            "token_refs_consistent": token_refs_consistent,
            "capability_invocation_ref": token.get("capability_invocation_ref") or "",
            "approval_request_ref": token.get("approval_request_ref") or "",
            "approval_request_artifact_path": token.get(
                "approval_request_artifact_path", ""
            ),
            "approval_request_tool": dict(_as_mapping(token.get("approval_request_tool"))),
        },
        "authority_boundary": {
            "capability_invocation_ref": selection.get("capability_invocation_ref") or "",
            "operator_facing_route": selection.get("operator_facing_route") or "",
            "requested_by": selection.get("requested_by") or "",
            "approval_request_ref": selection.get("approval_request_ref") or "",
            "approval_request_artifact_path": selection.get(
                "approval_request_artifact_path", ""
            ),
            "tool_confirmation_required": _as_mapping(
                selection.get("approval_request_tool")
            ).get("tool_confirmation_required")
            is True,
            "form2a_response_selected_in_artifact": selection.get(
                "form2a_response_selected_in_artifact"
            )
            is True,
            "form2a_response_selected_in_runtime": selection.get(
                "form2a_response_selected_in_runtime"
            )
            is True,
            "form2a_response_selection_progress_counted_in_artifact": selection.get(
                "form2a_response_selection_progress_counted_in_artifact"
            )
            is True,
            "form2a_response_selection_progress_counted_in_runtime": selection.get(
                "form2a_response_selection_progress_counted_in_runtime"
            )
            is True,
            "goal_640_progress_counted": selection.get("goal_640_progress_counted") is True,
            "intelligence_source": selection.get("intelligence_source"),
            "eligible_for_ai_agent_progress": selection.get(
                "eligible_for_ai_agent_progress"
            )
            is True,
            "ai_agent_progress_counted": selection.get("ai_agent_progress_counted")
            is True,
            "llm_judgment_in_gate": selection.get("llm_judgment_in_gate") is True,
            "llm_response_planner_status": selection.get("llm_response_planner_status")
            or "not_configured",
            "llm_response_proposal_present": bool(
                selection.get("llm_response_proposal_ref")
            ),
            "operator_approval_required": selection.get("operator_approval_required") is True,
            "operator_approval_token_issued_in_artifact": token.get(
                "operator_approval_token_issued_in_artifact"
            )
            is True,
            "operator_approval_token_consumed_in_runtime": token.get(
                "operator_approval_token_consumed_in_runtime"
            )
            is True,
            "dispatch_executed": selection.get("dispatch_executed_in_runtime") is True,
            "dispatch_executed_in_runtime": selection.get("dispatch_executed_in_runtime") is True,
            "automatic_dispatch_executed": selection.get("automatic_dispatch_executed") is True
            or token.get("automatic_dispatch_executed") is True,
            "physical_execution_invoked": selection.get("physical_execution_invoked") is True
            or token.get("physical_execution_invoked") is True,
            "hardware_target_allowed": selection.get("hardware_target_allowed") is True
            or token.get("hardware_target_allowed") is True,
            "core_direct_execution_used": selection.get("core_direct_execution_used") is True
            or token.get("core_direct_execution_used") is True,
            "llm_gate_judge_used": selection.get("llm_gate_judge_used") is True
            or token.get("llm_gate_judge_used") is True,
            "approval_free_stronger_execution": selection.get("approval_free_stronger_execution")
            is True
            or token.get("approval_free_stronger_execution") is True,
            "delivery_completion_claimed": selection.get("delivery_completion_claimed") is True
            or token.get("delivery_completion_claimed") is True,
            "public_sync_performed": selection.get("public_sync_performed") is True
            or token.get("public_sync_performed") is True,
            "source_form1_hash_matches": source_hash_matches,
            "token_refs_consistent": token_refs_consistent,
            "blocking_reasons": blocking_reasons,
        },
        "operator_note": (
            "This selects a Form 2a action from the source-bound Form 1 wind "
            "or payload behavior delta and issues an operator-approval token artifact. It does not "
            "consume the token or execute dispatch."
        ),
    }


def _form2a_review_status_label(review_status: str) -> str:
    normalized = review_status.strip().lower().replace("-", "_")
    if normalized in {"approve", "approved"}:
        return "approved"
    if normalized in {"reject", "rejected"}:
        return "rejected"
    if normalized in {"request_revision", "revision_requested", "revise"}:
        return "revision_requested"
    return "blocked"


def run_form2a_operator_review(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    review_status: str,
    operator_note: str = "",
    capability_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a human operator review of the latest Form 2a LLM proposal."""

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    form2a_summary = build_form2a_response_selection_summary(artifact_root=root)
    chain = _latest_form2a_response_selection_chain(root)
    selection_path_text, selection = chain["selection"]
    token_path_text, token = chain["token"]
    normalized_status = _form2a_review_status_label(review_status)
    generated_at = datetime.now(timezone.utc).isoformat()
    review_dir = _artifact_dir(root, "missionos_form2a_human_operator_review")
    review_path = review_dir / "missionos_form2a_human_operator_review.json"
    review_dir.mkdir(parents=True, exist_ok=True)
    selection_abs = _resolve_artifact_path(root, selection_path_text) if selection_path_text else Path("")
    token_abs = _resolve_artifact_path(root, token_path_text) if token_path_text else Path("")
    blocking_reasons: list[str] = []
    if form2a_summary.get("summary_status") != "form2a_response_selected":
        blocking_reasons.append("form2a_response_selection_not_ready")
    if not selection_path_text or not selection_abs.exists():
        blocking_reasons.append("form2a_response_selection_artifact_missing")
    elif not selection_abs.is_file():
        blocking_reasons.append("form2a_response_selection_artifact_not_file")
    if not token_path_text or not token_abs.exists():
        blocking_reasons.append("form2a_operator_approval_token_missing")
    elif not token_abs.is_file():
        blocking_reasons.append("form2a_operator_approval_token_not_file")
    if normalized_status == "blocked":
        blocking_reasons.append("human_operator_review_status_invalid")

    capability_context_payload = dict(
        _as_mapping(capability_context)
        or _as_mapping(selection.get("capability_invocation"))
        or _as_mapping(token.get("capability_invocation"))
        or capability_invocation_context(
            "form2a_operator_review",
            requested_by="direct_gateway_route",
            source_route="/missionos/form2a-operator-review/approve",
            request_payload={
                "review_status": normalized_status,
                "response_selection_artifact_path": selection_path_text,
                "operator_approval_token_artifact_path": token_path_text,
            },
        )
    )
    approval_request_tool = dict(
        _as_mapping(selection.get("approval_request_tool"))
        or _as_mapping(token.get("approval_request_tool"))
    )
    approval_granted = bool(normalized_status == "approved" and not blocking_reasons)
    review_id = f"missionos_form2a_human_operator_review_{uuid.uuid4().hex[:12]}"
    review = {
        "schema_version": FORM2A_HUMAN_OPERATOR_REVIEW_SCHEMA_VERSION,
        "review_id": review_id,
        "review_status": normalized_status if not blocking_reasons else "blocked",
        "generated_at": generated_at,
        "causal_form": "Form 2a human operator review" if approval_granted else "Form 0b",
        "progress_counted": False,
        "goal_640_progress_counted": False,
        "ai_agent_progress_counted": False,
        "drone_physics_affected": False,
        "human_operator_review_recorded_in_artifact": True,
        "human_operator_approval_granted_in_artifact": approval_granted,
        "human_operator_approval_granted_in_runtime": False,
        "human_operator_rejection_recorded_in_artifact": normalized_status == "rejected"
        and not blocking_reasons,
        "human_operator_revision_requested_in_artifact": normalized_status == "revision_requested"
        and not blocking_reasons,
        "operator_identity": "local_operator",
        "review_channel": "gateway_http_endpoint",
        "operator_note": operator_note,
        "capability_invocation": capability_context_payload,
        "capability_invocation_ref": capability_context_payload.get(
            "capability_invocation_ref", ""
        ),
        "operator_facing_route": capability_context_payload.get(
            "operator_facing_route", ""
        ),
        "requested_by": capability_context_payload.get("requested_by", ""),
        "approval_request_tool": approval_request_tool,
        "approval_request_ref": approval_request_tool.get("approval_request_ref", ""),
        "approval_request_artifact_path": approval_request_tool.get(
            "approval_request_artifact_path", ""
        ),
        "response_selection_ref": (
            f"missionos_form2a_response_selection:{selection.get('response_selection_id')}"
            if selection.get("response_selection_id")
            else ""
        ),
        "response_selection_artifact_path": selection_path_text,
        "response_selection_artifact_sha256": _sha256_file(selection_abs)
        if selection_abs.is_file()
        else "",
        "operator_approval_token_ref": token.get("approval_ref") or "",
        "operator_approval_token_artifact_path": token_path_text,
        "operator_approval_token_artifact_sha256": _sha256_file(token_abs)
        if token_abs.is_file()
        else "",
        "llm_response_proposal_ref": selection.get("llm_response_proposal_ref") or "",
        "llm_response_proposal_artifact_path": selection.get(
            "llm_response_proposal_artifact_path"
        )
        or "",
        "llm_response_approval_request": selection.get("llm_response_approval_request")
        or "",
        "selected_response_kind": selection.get("selected_response_kind") or "",
        "intelligence_source": selection.get("intelligence_source") or "",
        "eligible_for_ai_agent_progress": selection.get(
            "eligible_for_ai_agent_progress"
        )
        is True,
        "llm_judgment_in_gate": False,
        "operator_approved": False,
        "dispatch_authority_created": False,
        "operator_approval_token_consumed_in_runtime": False,
        "dispatch_executed_in_runtime": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "core_direct_execution_used": False,
        "llm_gate_judge_used": False,
        "approval_free_stronger_execution": False,
        "delivery_completion_claimed": False,
        "public_sync_performed": False,
        "blocking_reasons": blocking_reasons,
    }
    _write_artifact(review_path, review)
    return build_form2a_operator_review_summary(artifact_root=root)


def run_form2a_operator_review_approve(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    capability_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return run_form2a_operator_review(
        artifact_root=artifact_root,
        review_status="approved",
        capability_context=capability_context,
    )


def run_form2a_operator_review_reject(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    capability_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return run_form2a_operator_review(
        artifact_root=artifact_root,
        review_status="rejected",
        capability_context=capability_context,
    )


def run_form2a_operator_review_request_revision(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    capability_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return run_form2a_operator_review(
        artifact_root=artifact_root,
        review_status="revision_requested",
        capability_context=capability_context,
    )


def _form2a_human_operator_review_check(
    *,
    root: Path,
    selection_path: str,
    selection: Mapping[str, Any],
    token_path: str,
    token: Mapping[str, Any],
    review_path: str,
    review: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    selection_abs = _resolve_artifact_path(root, selection_path) if selection_path else Path("")
    token_abs = _resolve_artifact_path(root, token_path) if token_path else Path("")
    if not review_path:
        reasons.append("form2a_human_operator_review_missing")
    if review.get("schema_version") != FORM2A_HUMAN_OPERATOR_REVIEW_SCHEMA_VERSION:
        reasons.append("form2a_human_operator_review_schema_invalid")
    if review.get("review_status") != "approved":
        reasons.append(f"form2a_human_operator_review_not_approved:{review.get('review_status') or 'missing'}")
    if review.get("human_operator_approval_granted_in_artifact") is not True:
        reasons.append("human_operator_approval_granted_in_artifact_not_observed")
    expected_selection_ref = (
        f"missionos_form2a_response_selection:{selection.get('response_selection_id')}"
    )
    if review.get("response_selection_ref") != expected_selection_ref:
        reasons.append("form2a_human_operator_review_selection_ref_mismatch")
    if review.get("response_selection_artifact_path") != selection_path:
        reasons.append("form2a_human_operator_review_selection_path_mismatch")
    if selection_path:
        if not selection_abs.exists():
            reasons.append("form2a_human_operator_review_selection_artifact_missing")
        elif not selection_abs.is_file():
            reasons.append("form2a_human_operator_review_selection_artifact_not_file")
        elif review.get("response_selection_artifact_sha256") != _sha256_file(selection_abs):
            reasons.append("form2a_human_operator_review_selection_sha256_mismatch")
    if review.get("operator_approval_token_ref") != token.get("approval_ref"):
        reasons.append("form2a_human_operator_review_token_ref_mismatch")
    if review.get("operator_approval_token_artifact_path") != token_path:
        reasons.append("form2a_human_operator_review_token_path_mismatch")
    token_already_consumed = token.get("operator_approval_token_consumed_in_runtime") is True
    if token_path:
        if not token_abs.exists():
            reasons.append("form2a_human_operator_review_token_artifact_missing")
        elif not token_abs.is_file():
            reasons.append("form2a_human_operator_review_token_artifact_not_file")
        elif (
            not token_already_consumed
            and review.get("operator_approval_token_artifact_sha256") != _sha256_file(token_abs)
        ):
            reasons.append("form2a_human_operator_review_token_sha256_mismatch")
    if review.get("llm_judgment_in_gate") is True:
        reasons.append("form2a_human_operator_review_llm_judgment_in_gate")
    return {
        "approved": not reasons,
        "blocking_reasons": reasons,
        "review_path": review_path,
        "review_status": review.get("review_status") or "missing",
        "human_operator_approval_granted_in_artifact": review.get(
            "human_operator_approval_granted_in_artifact"
        )
        is True,
    }


def build_form2a_operator_review_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    root = Path(artifact_root)
    chain = _latest_form2a_response_selection_chain(root)
    selection_path, selection = chain["selection"]
    token_path, token = chain["token"]
    review_path, review_raw = chain["human_review"]
    review, runtime_blocks = _runtime_claims(review_raw)
    check = _form2a_human_operator_review_check(
        root=root,
        selection_path=selection_path,
        selection=selection,
        token_path=token_path,
        token=token,
        review_path=review_path,
        review=review,
    )
    blocking_reasons = list(check.get("blocking_reasons") or []) + runtime_blocks
    summary_status = (
        "approved"
        if review_path and check.get("approved") is True and not blocking_reasons
        else "review_recorded"
        if review_path and review.get("review_status") in {"rejected", "revision_requested"}
        else "blocked"
        if review_path
        else "missing"
    )
    return {
        "schema_version": FORM2A_HUMAN_OPERATOR_REVIEW_SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_status": summary_status,
        "classification": {
            "causal_form": "Form 2a human operator review"
            if summary_status == "approved"
            else "Form 0b",
            "progress_counted": False,
            "goal_640_progress_counted": False,
            "ai_agent_progress_counted": False,
            "drone_physics_affected": False,
        },
        "human_operator_review": {
            "status": review.get("review_status") or "missing",
            "artifact_path": review_path,
            "review_id": review.get("review_id"),
            "human_operator_review_recorded_in_artifact": review.get(
                "human_operator_review_recorded_in_artifact"
            )
            is True,
            "human_operator_approval_granted_in_artifact": review.get(
                "human_operator_approval_granted_in_artifact"
            )
            is True,
            "human_operator_approval_granted_in_runtime": review.get(
                "human_operator_approval_granted_in_runtime"
            )
            is True,
            "operator_identity": review.get("operator_identity"),
            "review_channel": review.get("review_channel"),
            "capability_invocation_ref": review.get("capability_invocation_ref") or "",
            "operator_facing_route": review.get("operator_facing_route") or "",
            "requested_by": review.get("requested_by") or "",
            "approval_request_ref": review.get("approval_request_ref") or "",
            "approval_request_artifact_path": review.get(
                "approval_request_artifact_path", ""
            ),
            "approval_request_tool": dict(
                _as_mapping(review.get("approval_request_tool"))
            ),
        },
        "source_selection": {
            "artifact_path": selection_path,
            "response_selection_ref": review.get("response_selection_ref") or "",
            "selected_response_kind": selection.get("selected_response_kind"),
            "intelligence_source": selection.get("intelligence_source"),
            "llm_response_proposal_ref": selection.get("llm_response_proposal_ref")
            or "",
            "llm_response_approval_request": selection.get(
                "llm_response_approval_request"
            )
            or "",
        },
        "operator_approval_token": {
            "artifact_path": token_path,
            "approval_ref": token.get("approval_ref"),
            "operator_approval_token_consumed_in_runtime": token.get(
                "operator_approval_token_consumed_in_runtime"
            )
            is True,
            "approval_request_ref": token.get("approval_request_ref") or "",
            "approval_request_artifact_path": token.get(
                "approval_request_artifact_path", ""
            ),
        },
        "authority_boundary": {
            "capability_invocation_ref": review.get("capability_invocation_ref") or "",
            "operator_facing_route": review.get("operator_facing_route") or "",
            "requested_by": review.get("requested_by") or "",
            "approval_request_ref": review.get("approval_request_ref") or "",
            "approval_request_artifact_path": review.get(
                "approval_request_artifact_path", ""
            ),
            "tool_confirmation_required": _as_mapping(
                review.get("approval_request_tool")
            ).get("tool_confirmation_required")
            is True,
            "human_operator_approval_granted_in_artifact": check.get(
                "human_operator_approval_granted_in_artifact"
            )
            is True,
            "human_operator_approval_verified_for_token_consumption": check.get(
                "approved"
            )
            is True,
            "operator_approval_token_consumed_in_runtime": token.get(
                "operator_approval_token_consumed_in_runtime"
            )
            is True,
            "dispatch_executed_in_runtime": False,
            "automatic_dispatch_executed": review.get("automatic_dispatch_executed")
            is True,
            "physical_execution_invoked": review.get("physical_execution_invoked") is True,
            "hardware_target_allowed": review.get("hardware_target_allowed") is True,
            "llm_gate_judge_used": review.get("llm_gate_judge_used") is True,
            "approval_free_stronger_execution": review.get(
                "approval_free_stronger_execution"
            )
            is True,
            "delivery_completion_claimed": review.get("delivery_completion_claimed")
            is True,
            "blocking_reasons": blocking_reasons,
        },
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _consume_form2a_operator_token(
    *,
    token_path: Path,
    selection: Mapping[str, Any],
) -> dict[str, Any]:
    lock_path = token_path.with_name(f"{token_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        token = _read_json(token_path) or {}
        before_sha256 = _sha256_file(token_path) if token_path.exists() else ""
        now = datetime.now(timezone.utc)
        expires_at = _parse_timestamp(token.get("expires_at"))
        blocking_reasons: list[str] = []
        if token.get("approval_token_status") != "issued_unconsumed":
            blocking_reasons.append("operator_approval_token_not_issued_unconsumed")
        if token.get("operator_approval_token_consumed_in_runtime") is True:
            blocking_reasons.append("operator_approval_token_replay_detected")
        if expires_at is None:
            blocking_reasons.append("operator_approval_token_expiry_missing_or_invalid")
        elif expires_at < now:
            blocking_reasons.append("operator_approval_token_expired")
        if token.get("response_selection_ref") != (
            f"missionos_form2a_response_selection:{selection.get('response_selection_id')}"
        ):
            blocking_reasons.append("operator_approval_token_selection_ref_mismatch")
        if token.get("bounded_action_ref") != selection.get("bounded_action_ref"):
            blocking_reasons.append("operator_approval_token_bounded_action_ref_mismatch")
        if token.get("dispatch_ref") != selection.get("dispatch_ref"):
            blocking_reasons.append("operator_approval_token_dispatch_ref_mismatch")
        if blocking_reasons:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return {
                "schema_version": "missionos_form2a_operator_token_consumption.v1",
                "consumption_status": "blocked",
                "consumed": False,
                "blocking_reasons": blocking_reasons,
                "token_replay_detected": "operator_approval_token_replay_detected"
                in blocking_reasons,
                "token_artifact_path": _repo_or_path_relative(token_path),
                "token_before_sha256": before_sha256,
                "fcntl_lock_used": True,
                "atomic_replace_used": False,
            }
        consumed_at = now.isoformat()
        consumption_id = f"missionos_form2a_token_consumption_{uuid.uuid4().hex[:12]}"
        updated = dict(token)
        updated.update(
            {
                "approval_token_status": "consumed_in_runtime",
                "operator_approval_token_consumed_in_runtime": True,
                "operator_approval_token_consumed_at": consumed_at,
                "token_consumer_process_pid": os.getpid(),
                "token_consumption_id": consumption_id,
                "token_replay_detected": False,
            }
        )
        _atomic_write_raw_json(token_path, updated)
        after_sha256 = _sha256_file(token_path)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return {
        "schema_version": "missionos_form2a_operator_token_consumption.v1",
        "consumption_status": "consumed",
        "consumed": True,
        "consumed_at": consumed_at,
        "consumption_id": consumption_id,
        "token_artifact_path": _repo_or_path_relative(token_path),
        "token_before_sha256": before_sha256,
        "token_after_sha256": after_sha256,
        "token_sha256_changed": before_sha256 != after_sha256,
        "token_replay_detected": False,
        "fcntl_lock_used": True,
        "atomic_replace_used": True,
        "consumer_process_pid": os.getpid(),
    }


def _form2a_reobservation_command() -> tuple[list[str], bool]:
    override = os.getenv(FORM2A_TRAJECTORY_REOBSERVATION_COMMAND_ENV)
    if override:
        return shlex.split(override), True
    return (
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "audit_mission_designer_wind_trajectory_delta.py"),
        ],
        False,
    )


def _form2a_reobservation_timeout_seconds() -> int:
    try:
        return int(os.getenv(FORM2A_TRAJECTORY_REOBSERVATION_TIMEOUT_ENV, ""))
    except ValueError:
        return DEFAULT_FORM2A_REOBSERVATION_TIMEOUT_SECONDS


def _latest_wind_delta_artifact(search_root: Path) -> tuple[dict[str, Any], str]:
    paths = sorted(
        search_root.rglob("drone_behavior_delta_under_wind.json"),
        key=lambda path: (path.stat().st_mtime, path.as_posix()),
        reverse=True,
    )
    for path in paths:
        payload = _read_json(path)
        if payload is not None:
            return payload, path.as_posix()
    return {}, ""


def _invoke_form2a_trajectory_reobservation(
    *,
    artifact_root: Path,
    selected_response_kind: str,
    response_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if os.getenv(FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV) != "1":
        return {
            "reobservation_invoked": False,
            "blocked_reason": f"{FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV}_not_enabled",
        }
    command, override_used = _form2a_reobservation_command()
    if override_used and os.getenv("MISSIONOS_ALLOW_RUNTIME_COMMAND_OVERRIDE") != "1":
        return {
            "reobservation_invoked": False,
            "blocked_reason": (
                f"{FORM2A_TRAJECTORY_REOBSERVATION_COMMAND_ENV}_requires_"
                "MISSIONOS_ALLOW_RUNTIME_COMMAND_OVERRIDE"
            ),
        }
    run_dir = artifact_root / "missionos_form2a_trajectory_reobservation" / (
        f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}"
    )
    output_dir = run_dir / "wind_trajectory_delta"
    command_argv = list(command)
    if not override_used:
        command_argv.extend(["--output-dir", str(output_dir), "--asymmetric-compensation"])
    run_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = env.get("PYTHONPATH") or str(REPO_ROOT)
    env["MISSIONOS_FORM2A_SELECTED_RESPONSE_KIND"] = selected_response_kind
    form2a_smoke_env = form2a_backend_action_smoke_env(
        selected_response_kind,
        response_parameters,
    )
    env.update(form2a_smoke_env)
    started_at = datetime.now(timezone.utc).isoformat()
    process = subprocess.Popen(
        command_argv,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=_form2a_reobservation_timeout_seconds())
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=30)
        stderr = f"{stderr}\nmissionos form2a trajectory reobservation timeout"
    completed_at = datetime.now(timezone.utc).isoformat()
    stdout_path = run_dir / "form2a_trajectory_reobservation_stdout.txt"
    stderr_path = run_dir / "form2a_trajectory_reobservation_stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    artifact, artifact_path = _latest_wind_delta_artifact(output_dir)
    if not artifact:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = {}
        artifact = parsed if isinstance(parsed, dict) else {}
        artifact_path = str(stdout_path) if artifact else ""
    evidence = {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": "subprocess",
        "invocation_target": "scripts/audit_mission_designer_wind_trajectory_delta.py"
        if not override_used
        else "override:missionos_form2a_trajectory_reobservation",
        "invocation_started_at": started_at,
        "invocation_completed_at": completed_at,
        "invocation_stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "invocation_stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        "invocation_exit_code": int(process.returncode if process.returncode is not None else -1),
        "process_pid": int(process.pid),
        "command_argv": command_argv,
        "form2a_backend_action_parameters": dict(response_parameters or {}),
        "form2a_smoke_env": form2a_smoke_env,
        "command_argv_sha256": hashlib.sha256(
            json.dumps(command_argv, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "backend_target": "px4_gazebo_sitl",
        "opt_in_env": True,
        "artifact_dir": str(run_dir),
        "stdout_artifact_path": str(stdout_path),
        "stderr_artifact_path": str(stderr_path),
        "trajectory_delta_artifact_path": artifact_path,
        "override_command_used": override_used,
    }
    return {
        "reobservation_invoked": True,
        "reobservation_source": "wind_trajectory_delta_audit_subprocess"
        if not override_used
        else "override_stdout_json",
        "form2a_smoke_env": form2a_smoke_env,
        "runtime_invocation_evidence": evidence,
        "trajectory_delta_artifact": artifact,
        "trajectory_delta_artifact_path": artifact_path,
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }


def _trajectory_reobservation_supports_goal(
    reobservation: Mapping[str, Any],
    *,
    delta_response_check: Mapping[str, Any] | None = None,
) -> bool:
    artifact = _as_mapping(reobservation.get("trajectory_delta_artifact"))
    evidence = _as_mapping(reobservation.get("runtime_invocation_evidence"))
    source_binding = _as_mapping(artifact.get("source_binding"))
    metrics = _as_mapping(artifact.get("metrics"))
    base_supported = bool(
        reobservation.get("reobservation_invoked") is True
        and reobservation.get("reobservation_source") == "wind_trajectory_delta_audit_subprocess"
        and evidence.get("invocation_exit_code") == 0
        and evidence.get("override_command_used") is not True
        and artifact.get("schema_version") == "drone_behavior_delta_under_wind.v1"
        and artifact.get("progress_counted") is True
        and artifact.get("drone_physics_affected") is True
        and artifact.get("causal_form") in {"Form 1a", "Form 1b"}
        and artifact.get("run_mode") == "executed_runs"
        and source_binding.get("runtime_invocation_evidence_complete") is True
        and float(metrics.get("max_observed_delta_m") or 0.0)
        >= float(metrics.get("delta_threshold_m") or 0.0)
        > 0.0
    )
    if not base_supported:
        return False
    if delta_response_check is not None:
        return delta_response_check.get("post_action_delta_comparison_satisfied") is True
    return True


def _payload_recovery_action_supports_goal(payload: Mapping[str, Any]) -> bool:
    checks = _as_mapping(payload.get("checks"))
    return bool(
        payload.get("schema_version")
        == "mission_designer_payload_recovery_action_audit.v1"
        and payload.get("audit_status") == "payload_recovery_action_observed"
        and payload.get("closed_loop_observed") is True
        and payload.get("form2a_action_supported") is True
        and checks.get("operator_approved_payload_recovery_action_observed") is True
        and checks.get("dropoff_not_claimed") is True
        and payload.get("delivery_completion_claimed") is False
        and payload.get("hardware_target_allowed") is False
        and payload.get("physical_execution_invoked") is False
    )


def _invoke_form2a_payload_recovery_action(
    *,
    artifact_root: Path,
    selection: Mapping[str, Any],
    response_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    if os.getenv("RUN_MISSIONOS_SITL_DISPATCH_RUNTIME") != "1":
        return {
            "payload_recovery_action_invoked": False,
            "blocked_reason": "RUN_MISSIONOS_SITL_DISPATCH_RUNTIME_not_enabled",
        }
    source_path_text = str(selection.get("input_form1_artifact_path") or "")
    if not source_path_text:
        return {
            "payload_recovery_action_invoked": False,
            "blocked_reason": "payload_form1_source_artifact_missing",
        }
    source_path = _resolve_repo_or_artifact_path(artifact_root, source_path_text)
    if not source_path.exists():
        return {
            "payload_recovery_action_invoked": False,
            "blocked_reason": "payload_form1_source_artifact_not_found",
            "input_form1_artifact_path": source_path_text,
        }
    try:
        payload_mass_kg = float(
            response_parameters.get("payload_mass_kg")
            or selection.get("source_heavy_payload_kg")
            or 1.25
        )
    except (TypeError, ValueError):
        payload_mass_kg = 1.25
    recovery_action = "land"
    run_dir = artifact_root / "missionos_form2a_payload_recovery_action" / (
        f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}"
    )
    command_argv = [
        sys.executable,
        str(PAYLOAD_RECOVERY_ACTION_AUDIT),
        "--advisory-artifact",
        str(source_path),
        "--payload-mass-kg",
        str(payload_mass_kg),
        "--recovery-action",
        recovery_action,
        "--output-dir",
        str(run_dir),
    ]
    run_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = env.get("PYTHONPATH") or str(REPO_ROOT)
    started_at = datetime.now(timezone.utc).isoformat()
    process = subprocess.Popen(
        command_argv,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=DEFAULT_FORM2A_REOBSERVATION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=30)
        stderr = f"{stderr}\nmissionos form2a payload recovery action timeout"
    completed_at = datetime.now(timezone.utc).isoformat()
    stdout_path = run_dir / "form2a_payload_recovery_action_stdout.txt"
    stderr_path = run_dir / "form2a_payload_recovery_action_stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    try:
        artifact = json.loads(stdout)
    except json.JSONDecodeError:
        artifact = {}
    artifact = artifact if isinstance(artifact, dict) else {}
    evidence = {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_kind": "subprocess",
        "invocation_target": "scripts/audit_mission_designer_payload_recovery_action.py",
        "invocation_started_at": started_at,
        "invocation_completed_at": completed_at,
        "invocation_stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "invocation_stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        "invocation_exit_code": int(process.returncode if process.returncode is not None else -1),
        "process_pid": int(process.pid),
        "command_argv": command_argv,
        "command_argv_sha256": hashlib.sha256(
            json.dumps(command_argv, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "backend_target": "px4_gazebo_sitl",
        "opt_in_env": True,
        "artifact_dir": str(run_dir),
        "stdout_artifact_path": str(stdout_path),
        "stderr_artifact_path": str(stderr_path),
        "form2a_backend_action_parameters": {
            "payload_mass_kg": payload_mass_kg,
            "recovery_action": recovery_action,
        },
    }
    return {
        "payload_recovery_action_invoked": True,
        "payload_recovery_action_source": "payload_recovery_action_audit_subprocess",
        "runtime_invocation_evidence": evidence,
        "payload_recovery_action_artifact": artifact,
        "payload_recovery_action_artifact_path": str(
            Path(artifact.get("audit_dir", run_dir))
            / "mission_designer_payload_recovery_action.json"
        )
        if artifact
        else "",
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }


def _post_action_delta_response_check(
    *,
    root: Path,
    selection: Mapping[str, Any],
    reobservation: Mapping[str, Any],
) -> dict[str, Any]:
    selected_response_kind = str(selection.get("selected_response_kind") or "")
    comparison_required = selected_response_kind in FORM2A_COMPENSATION_RESPONSE_KINDS
    comparison_skipped = selected_response_kind == FORM2A_WARNING_RESPONSE_KIND
    blocking_reasons: list[str] = []
    if selected_response_kind not in FORM2A_COMPENSATION_RESPONSE_KINDS and not comparison_skipped:
        blocking_reasons.append("form2a_selected_response_kind_not_supported_for_delta_check")
    improvement_gate = _form2a_improvement_gate(root)
    ratio_threshold = float(
        improvement_gate.get("ratio") or FORM2A_DEFAULT_IMPROVEMENT_GATE_RATIO
    )
    if comparison_required and improvement_gate.get("supported") is not True:
        blocking_reasons.extend(
            str(reason) for reason in improvement_gate.get("blocking_reasons") or []
        )

    source_path_text = str(selection.get("input_form1_artifact_path") or "")
    source_path = _resolve_repo_or_artifact_path(root, source_path_text) if source_path_text else None
    source_payload = _read_json(source_path) if source_path else None
    source_sha256_matches = bool(
        source_path
        and source_path.exists()
        and selection.get("input_form1_artifact_sha256") == _sha256_file(source_path)
    )
    if source_path is None or not source_path.exists():
        blocking_reasons.append("pre_action_form1_artifact_missing")
    if not source_sha256_matches:
        blocking_reasons.append("pre_action_form1_artifact_sha256_mismatch")

    pre_metrics = _as_mapping(_as_mapping(source_payload).get("metrics"))
    post_artifact = _as_mapping(reobservation.get("trajectory_delta_artifact"))
    post_metrics = _as_mapping(post_artifact.get("metrics"))
    try:
        pre_delta = float(pre_metrics.get("max_observed_delta_m"))
        post_delta = float(post_metrics.get("max_observed_delta_m"))
    except (TypeError, ValueError):
        pre_delta = 0.0
        post_delta = 0.0
        blocking_reasons.append("pre_or_post_action_delta_metric_missing")

    if pre_delta <= 0.0 and comparison_required:
        blocking_reasons.append("pre_action_delta_not_positive")
    ratio = post_delta / pre_delta if pre_delta > 0.0 else None
    reduced_or_equal = bool(pre_delta > 0.0 and post_delta <= pre_delta)
    improvement_exceeds_noise_floor = bool(
        improvement_gate.get("supported") is True
        and ratio is not None
        and ratio <= ratio_threshold
    )
    if comparison_required and not reduced_or_equal:
        blocking_reasons.append("post_action_delta_not_reduced_relative_to_pre_action")
    elif comparison_required and not improvement_exceeds_noise_floor:
        blocking_reasons.append("post_action_delta_within_noise_floor")

    comparison_satisfied = bool(
        not blocking_reasons
        and (
            comparison_skipped
            or (comparison_required and improvement_exceeds_noise_floor)
        )
    )
    return {
        "schema_version": "missionos_form2a_post_action_delta_response_check.v1",
        "selected_response_kind": selected_response_kind,
        "pre_action_form1_artifact_path": source_path_text,
        "pre_action_form1_artifact_sha256_matches": source_sha256_matches,
        "pre_action_max_observed_delta_m": pre_delta,
        "post_action_max_observed_delta_m": post_delta,
        "post_to_pre_delta_ratio": ratio,
        "post_to_pre_delta_ratio_observed": ratio,
        "post_to_pre_delta_ratio_threshold": ratio_threshold,
        "noise_floor_source_artifact_path": improvement_gate.get("noise_floor_source"),
        "noise_floor_source_artifact_sha256": improvement_gate.get(
            "noise_floor_source_artifact_sha256"
        ),
        "noise_floor_source_artifact_expected_sha256": improvement_gate.get(
            "noise_floor_source_artifact_expected_sha256"
        ),
        "noise_floor_source_artifact_sha256_matches": improvement_gate.get(
            "noise_floor_source_artifact_sha256_matches"
        )
        is True,
        "noise_floor_sample_count": improvement_gate.get("sample_count"),
        "noise_floor_fraction_95": improvement_gate.get("noise_fraction_95"),
        "post_action_delta_comparison_required": comparison_required,
        "post_action_delta_comparison_skipped": comparison_skipped,
        "post_action_reduces_trajectory_delta_relative_to_pre_action": reduced_or_equal
        if comparison_required
        else False,
        "post_action_improvement_exceeds_noise_floor": improvement_exceeds_noise_floor
        if comparison_required
        else False,
        "post_action_delta_comparison_satisfied": comparison_satisfied,
        "blocking_reasons": blocking_reasons,
    }


def _dispatch_summary_uses_real_smoke(root: Path, dispatch_summary: Mapping[str, Any]) -> bool:
    execution_path = str(_as_mapping(dispatch_summary.get("bounded_dispatch_execution")).get("artifact_path") or "")
    execution = _read_json(_resolve_artifact_path(root, execution_path)) if execution_path else None
    if not execution:
        return False
    return bool(
        execution.get("runtime_summary_source") == "smoke_summary_json"
        and execution.get("runtime_summary_source_path")
        and _as_mapping(execution.get("runtime_invocation_evidence")).get("invocation_exit_code") == 0
    )


def _latest_form2a_action_consumption(root: Path) -> tuple[str, dict[str, Any]]:
    latest = _latest_payloads(root, "missionos_form2a_action_consumption.json")
    return latest[0] if latest else ("", {})


def _latest_llm_repair_proposal(root: Path) -> tuple[str, dict[str, Any]]:
    latest = _latest_payloads(root, "missionos_llm_repair_proposal.json")
    return latest[0] if latest else ("", {})


def _remove_truthy_unsuffixed_runtime_claims(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _remove_truthy_unsuffixed_runtime_claims(nested)
            for key, nested in value.items()
            if not (key in MISSIONOS_RUNTIME_CLAIM_KEYS and nested is True)
        }
    if isinstance(value, list):
        return [_remove_truthy_unsuffixed_runtime_claims(item) for item in value]
    return value


def run_form2a_action_consumption(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Consume the Form 2a token, run SITL dispatch, and re-observe trajectory delta."""

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    form2a_summary = build_form2a_response_selection_summary(artifact_root=root)
    chain = _latest_form2a_response_selection_chain(root)
    selection_path, selection = chain["selection"]
    token_path_text, token = chain["token"]
    review_path_text, review = chain["human_review"]
    generated_at = datetime.now(timezone.utc).isoformat()
    action_dir = _artifact_dir(root, "missionos_form2a_action_consumption")
    action_path = action_dir / "missionos_form2a_action_consumption.json"
    action_dir.mkdir(parents=True, exist_ok=True)
    blocking_reasons: list[str] = []
    form2a_blocking_reasons = list(
        _as_mapping(form2a_summary.get("authority_boundary")).get("blocking_reasons")
        or []
    )
    selection_blocked_only_because_token_was_consumed = bool(
        selection_path
        and token_path_text
        and form2a_blocking_reasons
        and set(form2a_blocking_reasons) == {"operator_approval_token_unconsumed_not_observed"}
    )
    if (
        form2a_summary.get("summary_status") != "form2a_response_selected"
        and not selection_blocked_only_because_token_was_consumed
    ):
        blocking_reasons.append("form2a_response_selection_not_ready")
    if not token_path_text:
        blocking_reasons.append("form2a_operator_approval_token_missing")
    human_review_check = _form2a_human_operator_review_check(
        root=root,
        selection_path=selection_path,
        selection=selection,
        token_path=token_path_text,
        token=token,
        review_path=review_path_text,
        review=review,
    )
    blocking_reasons.extend(
        str(reason) for reason in human_review_check.get("blocking_reasons") or []
    )
    if os.getenv("RUN_MISSIONOS_SITL_DISPATCH_RUNTIME") != "1":
        blocking_reasons.append("RUN_MISSIONOS_SITL_DISPATCH_RUNTIME_not_enabled")
    selected_response_kind = str(selection.get("selected_response_kind") or "")
    payload_recovery_requested = selected_response_kind == FORM2A_PAYLOAD_RECOVERY_RESPONSE_KIND
    if (
        not payload_recovery_requested
        and os.getenv(FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV) != "1"
    ):
        blocking_reasons.append(f"{FORM2A_TRAJECTORY_REOBSERVATION_OPT_IN_ENV}_not_enabled")

    token_path = _resolve_artifact_path(root, token_path_text) if token_path_text else Path("")
    token_consumption: Mapping[str, Any] = {}
    dispatch_summary: Mapping[str, Any] = {}
    reobservation: Mapping[str, Any] = {}
    payload_recovery_action: Mapping[str, Any] = {}
    delta_response_check: Mapping[str, Any] = {}
    response_parameters = _as_mapping(selection.get("llm_response_parameters"))
    token_consumed = False
    if not blocking_reasons and token_path.exists():
        token_consumption = _consume_form2a_operator_token(
            token_path=token_path,
            selection=selection,
        )
        token_consumed = token_consumption.get("consumed") is True
        blocking_reasons.extend(str(reason) for reason in token_consumption.get("blocking_reasons") or [])
    elif token_path_text and not token_path.exists():
        blocking_reasons.append("form2a_operator_approval_token_path_missing")

    if token_consumed and not blocking_reasons:
        if payload_recovery_requested:
            payload_recovery_action = _invoke_form2a_payload_recovery_action(
                artifact_root=root,
                selection=selection,
                response_parameters=response_parameters,
            )
            if not _payload_recovery_action_supports_goal(
                _as_mapping(payload_recovery_action.get("payload_recovery_action_artifact"))
            ):
                blocking_reasons.append("payload_recovery_action_not_observed")
        else:
            dispatch_summary = run_sitl_bounded_dispatch_execution(
                artifact_root=root,
                backend_action=str(selection.get("selected_response_kind") or "operator_gated_form2a_action"),
                backend_action_parameters=response_parameters,
            )
            if dispatch_summary.get("summary_status") != "executed_observed":
                blocking_reasons.append("form2a_dispatch_runtime_not_observed")
            elif not _dispatch_summary_uses_real_smoke(root, dispatch_summary):
                blocking_reasons.append("form2a_dispatch_runtime_smoke_source_not_observed")
            else:
                reobservation = _invoke_form2a_trajectory_reobservation(
                    artifact_root=root,
                    selected_response_kind=str(selection.get("selected_response_kind") or ""),
                    response_parameters=response_parameters,
                )
                if not _trajectory_reobservation_supports_goal(reobservation):
                    blocking_reasons.append("post_action_trajectory_delta_reobservation_not_observed")
                else:
                    delta_response_check = _post_action_delta_response_check(
                        root=root,
                        selection=selection,
                        reobservation=reobservation,
                    )
                    if delta_response_check.get("post_action_delta_comparison_satisfied") is not True:
                        blocking_reasons.extend(
                            str(reason)
                            for reason in delta_response_check.get("blocking_reasons") or []
                        )

    payload_recovery_supported = _payload_recovery_action_supports_goal(
        _as_mapping(payload_recovery_action.get("payload_recovery_action_artifact"))
    )
    dispatch_executed_in_artifact = bool(dispatch_summary or payload_recovery_action)
    dispatch_execution_summary_status = (
        dispatch_summary.get("summary_status", "")
        if dispatch_summary
        else "payload_recovery_action_observed"
        if payload_recovery_supported
        else "payload_recovery_action_blocked"
        if payload_recovery_action
        else "not_run"
    )
    dispatch_runtime_observed = bool(
        dispatch_summary.get("summary_status") == "executed_observed"
        or payload_recovery_supported
    )
    real_dispatch_smoke = bool(
        (
            dispatch_runtime_observed
            and _dispatch_summary_uses_real_smoke(root, dispatch_summary)
        )
        or payload_recovery_supported
    )
    trajectory_reobserved = bool(
        payload_recovery_supported
        or _trajectory_reobservation_supports_goal(
            reobservation,
            delta_response_check=delta_response_check,
        )
    )
    intelligence_source = str(selection.get("intelligence_source") or "")
    eligible_for_ai_agent_progress = bool(
        selection.get("eligible_for_ai_agent_progress") is True
        and intelligence_source == AI_AGENT_PROGRESS_ELIGIBLE_INTELLIGENCE_SOURCE
    )
    goal_progress = bool(token_consumed and real_dispatch_smoke and trajectory_reobserved)
    execution_path = str(_as_mapping(dispatch_summary.get("bounded_dispatch_execution")).get("artifact_path") or "")
    execution_payload = _read_json(_resolve_artifact_path(root, execution_path)) if execution_path else None
    payload_runtime_evidence = _as_mapping(
        payload_recovery_action.get("runtime_invocation_evidence")
    )
    runtime_fields = {}
    if execution_payload and isinstance(execution_payload.get("runtime_invocation_evidence"), Mapping):
        runtime_fields["runtime_invocation_evidence"] = execution_payload["runtime_invocation_evidence"]
    elif payload_runtime_evidence:
        runtime_fields["runtime_invocation_evidence"] = dict(payload_runtime_evidence)
    dispatch_runtime_env = _as_mapping(
        _as_mapping(runtime_fields.get("runtime_invocation_evidence")).get("form2a_smoke_env")
    )
    reobservation_runtime_env = _as_mapping(
        reobservation.get("form2a_smoke_env")
    ) or _as_mapping(
        _as_mapping(reobservation.get("runtime_invocation_evidence")).get("form2a_smoke_env")
    )
    llm_parameters_bound_to_runtime = (
        _llm_payload_parameters_bound_to_runtime_evidence(
            response_parameters,
            payload_runtime_evidence,
        )
        if payload_recovery_requested
        else _llm_response_parameters_bound_to_runtime_env(
            response_parameters,
            dispatch_runtime_env,
            reobservation_runtime_env,
        )
    )
    ai_agent_progress = bool(
        goal_progress
        and eligible_for_ai_agent_progress
        and llm_parameters_bound_to_runtime
    )
    action = {
        "schema_version": FORM2A_ACTION_CONSUMMATION_SCHEMA_VERSION,
        "action_consumption_id": f"missionos_form2a_action_consumption_{uuid.uuid4().hex[:12]}",
        "action_status": "goal_640_progress_observed"
        if goal_progress
        else "blocked"
        if blocking_reasons
        else "dispatch_observed_without_goal_progress",
        "generated_at": generated_at,
        "causal_form": "Form 2a action consummation" if goal_progress else "Form 0b",
        "progress_counted": goal_progress,
        "goal_640_progress_counted": goal_progress,
        "form2a_action_consumed_in_runtime": bool(token_consumed and dispatch_runtime_observed),
        "operator_approval_token_consumed_in_runtime": token_consumed,
        "human_operator_review_artifact_path": review_path_text,
        "human_operator_review_status": human_review_check.get("review_status"),
        "human_operator_approval_granted_in_artifact": human_review_check.get(
            "human_operator_approval_granted_in_artifact"
        )
        is True,
        "human_operator_approval_verified_for_token_consumption": human_review_check.get(
            "approved"
        )
        is True,
        "form2a_response_selection_artifact_path": selection_path,
        "operator_approval_token_artifact_path": token_path_text,
        "token_consumption_evidence": dict(token_consumption),
        "selected_response_kind": selection.get("selected_response_kind"),
        "llm_response_parameters": dict(
            response_parameters
        ),
        "llm_response_parameters_bound_to_runtime": bool(
            selection.get("intelligence_source")
            == AI_AGENT_PROGRESS_ELIGIBLE_INTELLIGENCE_SOURCE
            and llm_parameters_bound_to_runtime
        ),
        "form2a_runtime_smoke_env": dict(dispatch_runtime_env),
        "form2a_dispatch_runtime_smoke_env": dict(dispatch_runtime_env),
        "form2a_reobservation_runtime_smoke_env": dict(reobservation_runtime_env),
        "intelligence_source": selection.get("intelligence_source"),
        "eligible_for_ai_agent_progress": eligible_for_ai_agent_progress,
        "ai_agent_progress_counted": ai_agent_progress,
        "llm_judgment_in_gate": selection.get("llm_judgment_in_gate") is True,
        "bounded_action_ref": selection.get("bounded_action_ref"),
        "dispatch_ref": selection.get("dispatch_ref"),
        "dispatch_execution_summary_status": dispatch_execution_summary_status,
        "dispatch_execution_summary": _remove_truthy_unsuffixed_runtime_claims(
            dict(dispatch_summary)
        )
        if dispatch_summary
        else {},
        "payload_recovery_action_summary": dict(payload_recovery_action),
        "payload_recovery_action_supported": payload_recovery_supported,
        "dispatch_executed_in_artifact": dispatch_executed_in_artifact,
        "dispatch_executed_in_runtime": dispatch_runtime_observed,
        "outcome_observed_in_runtime": bool(
            _as_mapping(dispatch_summary.get("dispatch_outcome_observation")).get(
                "outcome_observed_in_runtime"
            )
            is True
            or payload_recovery_supported
        ),
        "verified_dispatch_execution_in_runtime": _as_mapping(
            dispatch_summary.get("recovery_verifier_result")
        ).get("verified_dispatch_execution_in_runtime")
        is True
        or payload_recovery_supported,
        "dispatch_runtime_smoke_source_observed": real_dispatch_smoke,
        "post_action_trajectory_delta_reobservation": dict(reobservation) if reobservation else {},
        "post_action_delta_response_check": dict(delta_response_check)
        if delta_response_check
        else {},
        "post_action_trajectory_delta_reobserved": trajectory_reobserved,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "core_direct_execution_used": False,
        "llm_gate_judge_used": False,
        "approval_free_stronger_execution": False,
        "delivery_completion_claimed": False,
        "public_sync_performed": False,
        "drone_physics_affected": goal_progress,
        "blocking_reasons": blocking_reasons,
        "next_required_applicator": "scoped_form3_two_cycle_runtime"
        if goal_progress
        else "payload_recovery_action_runtime_recheck"
        if payload_recovery_requested
        else "real_smoke_dispatch_and_post_action_trajectory_reobservation",
        **runtime_fields,
    }
    _write_artifact(action_path, action)
    return build_form2a_action_consumption_summary(artifact_root=root)


def build_form2a_action_consumption_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    root = Path(artifact_root)
    action_path, action_raw = _latest_form2a_action_consumption(root)
    action, runtime_blocks = _runtime_claims(action_raw)
    goal_progress = action.get("goal_640_progress_counted") is True
    token_consumed = action.get("operator_approval_token_consumed_in_runtime") is True
    dispatch_runtime_observed = action.get("dispatch_executed_in_runtime") is True
    dispatch_smoke_observed = action.get("dispatch_runtime_smoke_source_observed") is True
    trajectory_reobserved = action.get("post_action_trajectory_delta_reobserved") is True
    forbidden_true = [
        key
        for key in (
            "automatic_dispatch_executed",
            "physical_execution_invoked",
            "hardware_target_allowed",
            "core_direct_execution_used",
            "llm_gate_judge_used",
            "approval_free_stronger_execution",
            "delivery_completion_claimed",
            "public_sync_performed",
        )
        if action.get(key) is True
    ]
    blocking_reasons = list(action.get("blocking_reasons") or [])
    blocking_reasons.extend(forbidden_true)
    blocking_reasons.extend(runtime_blocks)
    summary_status = (
        "goal_640_progress_observed"
        if action_path and goal_progress and not blocking_reasons
        else "blocked"
        if action_path
        else "missing"
    )
    return {
        "schema_version": FORM2A_ACTION_CONSUMMATION_SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_status": summary_status,
        "classification": {
            "causal_form": "Form 2a action consummation" if goal_progress else "Form 0b",
            "surface": "Form 2a token consumption, SITL runtime action, and verifier/reobservation",
            "progress_counted": goal_progress,
            "goal_640_progress_counted": goal_progress,
            "dispatch_execution_progress_counted": bool(
                dispatch_runtime_observed and dispatch_smoke_observed
            ),
            "closed_loop_runtime_progress_counted": False,
            "drone_physics_affected": goal_progress,
            "ai_agent_progress_counted": action.get("ai_agent_progress_counted") is True,
        },
        "action_consumption": {
            "status": action.get("action_status") or "missing",
            "artifact_path": action_path,
            "operator_approval_token_consumed_in_runtime": token_consumed,
            "human_operator_review_status": action.get("human_operator_review_status"),
            "human_operator_approval_granted_in_artifact": action.get(
                "human_operator_approval_granted_in_artifact"
            )
            is True,
            "human_operator_approval_verified_for_token_consumption": action.get(
                "human_operator_approval_verified_for_token_consumption"
            )
            is True,
            "form2a_action_consumed_in_runtime": action.get("form2a_action_consumed_in_runtime") is True,
            "selected_response_kind": action.get("selected_response_kind"),
            "llm_response_parameters": dict(
                _as_mapping(action.get("llm_response_parameters"))
            ),
            "llm_response_parameters_bound_to_runtime": action.get(
                "llm_response_parameters_bound_to_runtime"
            )
            is True,
            "form2a_runtime_smoke_env": dict(
                _as_mapping(action.get("form2a_runtime_smoke_env"))
            ),
            "form2a_dispatch_runtime_smoke_env": dict(
                _as_mapping(action.get("form2a_dispatch_runtime_smoke_env"))
            ),
            "form2a_reobservation_runtime_smoke_env": dict(
                _as_mapping(action.get("form2a_reobservation_runtime_smoke_env"))
            ),
            "payload_recovery_action_supported": action.get(
                "payload_recovery_action_supported"
            )
            is True,
            "intelligence_source": action.get("intelligence_source"),
            "eligible_for_ai_agent_progress": action.get(
                "eligible_for_ai_agent_progress"
            )
            is True,
            "ai_agent_progress_counted": action.get("ai_agent_progress_counted") is True,
            "llm_judgment_in_gate": action.get("llm_judgment_in_gate") is True,
            "bounded_action_ref": action.get("bounded_action_ref"),
            "dispatch_ref": action.get("dispatch_ref"),
        },
        "runtime_dispatch": {
            "summary_status": action.get("dispatch_execution_summary_status") or "not_run",
            "dispatch_executed_in_runtime": dispatch_runtime_observed,
            "outcome_observed_in_runtime": action.get("outcome_observed_in_runtime") is True,
            "verified_dispatch_execution_in_runtime": action.get(
                "verified_dispatch_execution_in_runtime"
            )
            is True,
            "dispatch_runtime_smoke_source_observed": action.get(
                "dispatch_runtime_smoke_source_observed"
            )
            is True,
            "payload_recovery_action_supported": action.get(
                "payload_recovery_action_supported"
            )
            is True,
        },
        "trajectory_reobservation": {
            "status": "observed" if trajectory_reobserved else "not_observed",
            "post_action_trajectory_delta_reobserved": trajectory_reobserved,
            "artifact_path": _as_mapping(
                action.get("post_action_trajectory_delta_reobservation")
            ).get("trajectory_delta_artifact_path"),
            "post_action_delta_response_check": _as_mapping(
                action.get("post_action_delta_response_check")
            ),
        },
        "authority_boundary": {
            "human_operator_approval_granted_in_artifact": action.get(
                "human_operator_approval_granted_in_artifact"
            )
            is True,
            "human_operator_approval_verified_for_token_consumption": action.get(
                "human_operator_approval_verified_for_token_consumption"
            )
            is True,
            "operator_approval_token_consumed_in_runtime": token_consumed,
            "dispatch_executed_in_runtime": dispatch_runtime_observed,
            "outcome_observed_in_runtime": action.get("outcome_observed_in_runtime") is True,
            "verified_dispatch_execution_in_runtime": action.get(
                "verified_dispatch_execution_in_runtime"
            )
            is True,
            "post_action_trajectory_delta_reobserved": trajectory_reobserved,
            "goal_640_progress_counted": goal_progress,
            "intelligence_source": action.get("intelligence_source"),
            "eligible_for_ai_agent_progress": action.get(
                "eligible_for_ai_agent_progress"
            )
            is True,
            "ai_agent_progress_counted": action.get("ai_agent_progress_counted") is True,
            "llm_judgment_in_gate": action.get("llm_judgment_in_gate") is True,
            "automatic_dispatch_executed": action.get("automatic_dispatch_executed") is True,
            "physical_execution_invoked": action.get("physical_execution_invoked") is True,
            "hardware_target_allowed": action.get("hardware_target_allowed") is True,
            "core_direct_execution_used": action.get("core_direct_execution_used") is True,
            "llm_gate_judge_used": action.get("llm_gate_judge_used") is True,
            "approval_free_stronger_execution": action.get("approval_free_stronger_execution") is True,
            "delivery_completion_claimed": action.get("delivery_completion_claimed") is True,
            "public_sync_performed": action.get("public_sync_performed") is True,
            "blocking_reasons": blocking_reasons,
        },
        "operator_note": (
            "This consumed the Form 2a approval token, executed the SITL dispatch "
            "through the real smoke path, and re-observed trajectory delta. It "
            "counts as the next #640 progress point."
            if goal_progress
            else "This has not yet met the full token-consume, real smoke dispatch, "
            "and post-action trajectory reobservation bar for #640 progress."
        ),
    }


def run_llm_repair_planner_from_latest_evidence(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    capability_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask ADK/Gemini for a repair proposal from the latest MissionOS evidence."""

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    evidence_path_text, evidence = _latest_form2a_action_consumption(root)
    if not evidence_path_text:
        return build_llm_repair_planner_summary(artifact_root=root)
    evidence_path = _resolve_artifact_path(root, evidence_path_text)
    if not evidence_path.exists():
        return build_llm_repair_planner_summary(artifact_root=root)
    capability_context_payload = dict(
        _as_mapping(capability_context)
        or capability_invocation_context(
            "llm_repair_planning",
            requested_by="direct_gateway_route",
            source_route="/missionos/llm-repair-planner/run",
            request_payload={
                "input_evidence_artifact_path": evidence_path_text,
                "input_evidence_artifact_sha256": _sha256_file(evidence_path),
            },
        )
    )
    planner_result = run_llm_repair_planner(
        evidence_artifact=evidence,
        evidence_artifact_path=evidence_path_text,
        evidence_artifact_sha256=_sha256_file(evidence_path),
        artifact_root=root,
        artifact_relative=_relative,
    )
    _attach_capability_context_to_artifact(
        root=root,
        artifact_path=str(planner_result.get("proposal_artifact_path") or ""),
        capability_context=capability_context_payload,
    )
    if planner_result.get("planner_status") != "proposal_guardrail_passed":
        blocked_dir = _artifact_dir(root, "missionos_llm_repair_proposal")
        blocked_path = blocked_dir / "missionos_llm_repair_proposal.json"
        blocked_dir.mkdir(parents=True, exist_ok=True)
        blocked = {
            "schema_version": "missionos_llm_repair_proposal_blocked.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "planner_status": planner_result.get("planner_status") or "blocked",
            "blocking_reasons": list(planner_result.get("blocking_reasons") or []),
            "input_evidence_artifact_path": evidence_path_text,
            "input_evidence_artifact_sha256": _sha256_file(evidence_path),
            "capability_invocation": capability_context_payload,
            "capability_invocation_ref": capability_context_payload.get(
                "capability_invocation_ref", ""
            ),
            "operator_facing_route": capability_context_payload.get(
                "operator_facing_route", ""
            ),
            "requested_by": capability_context_payload.get("requested_by", ""),
            "intelligence_source": REPAIR_PLANNER_INTELLIGENCE_SOURCE,
            "llm_judgment_in_gate": False,
            "progress_counted": False,
            "goal_640_progress_counted": False,
            "ai_agent_progress_counted": False,
            "drone_physics_affected": False,
            "dispatch_authority_created": False,
            "operator_approved": False,
            "automatic_dispatch_executed": False,
            "physical_execution_invoked": False,
            "hardware_target_allowed": False,
            "delivery_completion_claimed": False,
            "guardrail": dict(_as_mapping(planner_result.get("guardrail"))),
        }
        _write_artifact(blocked_path, blocked)
    return build_llm_repair_planner_summary(artifact_root=root)


def run_llm_repair_planner_from_evidence_payload(
    *,
    evidence_artifact: Mapping[str, Any],
    evidence_label: str = "missionos_runtime_evidence",
    artifact_root: Path | str = ARTIFACT_ROOT,
    capability_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask ADK/Gemini for repair from a source-bound runtime evidence payload."""

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    evidence_dir = _artifact_dir(root, "missionos_repair_input_evidence")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "missionos_repair_input_evidence.json"
    capability_context_payload = dict(
        _as_mapping(capability_context)
        or capability_invocation_context(
            "llm_repair_planning",
            requested_by="direct_gateway_route",
            source_route="/missionos/llm-repair-planner/run-for-task",
            request_payload={
                "evidence_label": evidence_label,
                "summary_status": evidence_artifact.get("summary_status")
                or evidence_artifact.get("task_status")
                or evidence_artifact.get("status")
                or "runtime_evidence_attached",
            },
        )
    )
    evidence_payload = {
        "schema_version": "missionos_repair_input_evidence.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evidence_label": evidence_label,
        "summary_status": (
            evidence_artifact.get("summary_status")
            or evidence_artifact.get("task_status")
            or evidence_artifact.get("status")
            or "runtime_evidence_attached"
        ),
        "blocking_reasons": list(evidence_artifact.get("blocking_reasons") or []),
        "evidence_artifact": dict(evidence_artifact),
        "capability_invocation": capability_context_payload,
        "capability_invocation_ref": capability_context_payload.get(
            "capability_invocation_ref", ""
        ),
        "operator_facing_route": capability_context_payload.get(
            "operator_facing_route", ""
        ),
        "requested_by": capability_context_payload.get("requested_by", ""),
        "llm_judgment_in_gate": False,
        "progress_counted": False,
        "goal_640_progress_counted": False,
        "ai_agent_progress_counted": False,
        "drone_physics_affected": False,
        "dispatch_authority_created": False,
        "operator_approved": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
    }
    _write_artifact(evidence_path, evidence_payload, validate_progress=False)
    evidence_path_text = _repo_or_path_relative(evidence_path)
    planner_result = run_llm_repair_planner(
        evidence_artifact=evidence_payload,
        evidence_artifact_path=evidence_path_text,
        evidence_artifact_sha256=_sha256_file(evidence_path),
        artifact_root=root,
        artifact_relative=_relative,
    )
    _attach_capability_context_to_artifact(
        root=root,
        artifact_path=str(planner_result.get("proposal_artifact_path") or ""),
        capability_context=capability_context_payload,
    )
    if planner_result.get("planner_status") != "proposal_guardrail_passed":
        blocked_dir = _artifact_dir(root, "missionos_llm_repair_proposal")
        blocked_path = blocked_dir / "missionos_llm_repair_proposal.json"
        blocked_dir.mkdir(parents=True, exist_ok=True)
        blocked = {
            "schema_version": "missionos_llm_repair_proposal_blocked.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "planner_status": planner_result.get("planner_status") or "blocked",
            "blocking_reasons": list(planner_result.get("blocking_reasons") or []),
            "input_evidence_artifact_path": evidence_path_text,
            "input_evidence_artifact_sha256": _sha256_file(evidence_path),
            "capability_invocation": capability_context_payload,
            "capability_invocation_ref": capability_context_payload.get(
                "capability_invocation_ref", ""
            ),
            "operator_facing_route": capability_context_payload.get(
                "operator_facing_route", ""
            ),
            "requested_by": capability_context_payload.get("requested_by", ""),
            "intelligence_source": REPAIR_PLANNER_INTELLIGENCE_SOURCE,
            "llm_judgment_in_gate": False,
            "progress_counted": False,
            "goal_640_progress_counted": False,
            "ai_agent_progress_counted": False,
            "drone_physics_affected": False,
            "dispatch_authority_created": False,
            "operator_approved": False,
            "automatic_dispatch_executed": False,
            "physical_execution_invoked": False,
            "hardware_target_allowed": False,
            "delivery_completion_claimed": False,
            "guardrail": dict(_as_mapping(planner_result.get("guardrail"))),
        }
        _write_artifact(blocked_path, blocked)
    return build_llm_repair_planner_summary(artifact_root=root)


def build_llm_repair_planner_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Summarize the latest LLM repair proposal without execution authority."""

    root = Path(artifact_root)
    proposal_path_text, proposal_raw = _latest_llm_repair_proposal(root)
    proposal, runtime_blocks = _runtime_claims(proposal_raw)
    input_path_text = str(proposal.get("input_evidence_artifact_path") or "")
    input_path = _resolve_artifact_path(root, input_path_text) if input_path_text else None
    input_hash_matches = bool(
        input_path
        and input_path.exists()
        and proposal.get("input_evidence_artifact_sha256") == _sha256_file(input_path)
    )
    forbidden_true = [
        key
        for key in (
            "dispatch_authority_created",
            "operator_approved",
            "automatic_dispatch_executed",
            "dispatch_executed_in_runtime",
            "physical_execution_invoked",
            "hardware_target_allowed",
            "delivery_completion_claimed",
            "llm_judgment_in_gate",
            "progress_counted",
            "goal_640_progress_counted",
            "ai_agent_progress_counted",
            "drone_physics_affected",
        )
        if proposal.get(key) is True
    ]
    blocking_reasons: list[str] = []
    if proposal_path_text and proposal.get("schema_version") != "missionos_llm_repair_proposal.v1":
        blocking_reasons.append("llm_repair_proposal_schema_invalid")
        blocking_reasons.extend(str(reason) for reason in proposal.get("blocking_reasons") or [])
        guardrail = _as_mapping(proposal.get("guardrail"))
        blocking_reasons.extend(str(reason) for reason in guardrail.get("blocking_reasons") or [])
    if proposal_path_text and not input_hash_matches:
        blocking_reasons.append("input_evidence_artifact_sha256_mismatch")
    blocking_reasons.extend(forbidden_true)
    blocking_reasons.extend(runtime_blocks)
    summary_status = (
        "repair_proposal_ready"
        if proposal_path_text and not blocking_reasons
        else "blocked"
        if proposal_path_text
        else "missing"
    )
    return {
        "schema_version": LLM_REPAIR_PLANNER_SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_status": summary_status,
        "classification": {
            "causal_form": "Form 0b",
            "surface": "LLM repair proposal only; no approval, dispatch, or runtime progress",
            "progress_counted": False,
            "goal_640_progress_counted": False,
            "ai_agent_progress_counted": False,
            "drone_physics_affected": False,
        },
        "repair_proposal": {
            "artifact_path": proposal_path_text,
            "proposal_id": proposal.get("proposal_id"),
            "planner_status": proposal.get("planner_status")
            or ("proposal_guardrail_passed" if summary_status == "repair_proposal_ready" else "missing"),
            "intelligence_source": REPAIR_PLANNER_INTELLIGENCE_SOURCE,
            "repair_target": proposal.get("repair_target") or "",
            "repair_actions": list(proposal.get("repair_actions") or []),
            "rationale": proposal.get("rationale") or "",
            "expected_outcome": proposal.get("expected_outcome") or "",
            "uncertainty": proposal.get("uncertainty") or "",
            "next_verification": proposal.get("next_verification") or "",
            "proposed_operator_instruction": proposal.get("proposed_operator_instruction") or "",
            "proposed_parameters": dict(_as_mapping(proposal.get("proposed_parameters"))),
            "capability_invocation_ref": proposal.get("capability_invocation_ref") or "",
            "capability_id": _as_mapping(proposal.get("capability_invocation")).get(
                "capability_id"
            )
            or "",
            "operator_facing_route": proposal.get("operator_facing_route") or "",
            "requested_by": proposal.get("requested_by") or "",
        },
        "input_evidence": {
            "artifact_path": input_path_text,
            "artifact_sha256": proposal.get("input_evidence_artifact_sha256") or "",
            "artifact_sha256_matches_current_file": input_hash_matches,
        },
        "authority_boundary": {
            "capability_invocation_ref": proposal.get("capability_invocation_ref") or "",
            "operator_facing_route": proposal.get("operator_facing_route") or "",
            "requested_by": proposal.get("requested_by") or "",
            "llm_judgment_in_gate": proposal.get("llm_judgment_in_gate") is True,
            "dispatch_authority_created": proposal.get("dispatch_authority_created") is True,
            "operator_approved": proposal.get("operator_approved") is True,
            "automatic_dispatch_executed": proposal.get("automatic_dispatch_executed") is True,
            "dispatch_executed_in_runtime": proposal.get("dispatch_executed_in_runtime") is True,
            "physical_execution_invoked": proposal.get("physical_execution_invoked") is True,
            "hardware_target_allowed": proposal.get("hardware_target_allowed") is True,
            "delivery_completion_claimed": proposal.get("delivery_completion_claimed") is True,
            "progress_counted": proposal.get("progress_counted") is True,
            "goal_640_progress_counted": proposal.get("goal_640_progress_counted") is True,
            "ai_agent_progress_counted": proposal.get("ai_agent_progress_counted") is True,
            "drone_physics_affected": proposal.get("drone_physics_affected") is True,
            "blocking_reasons": blocking_reasons,
        },
        "operator_note": (
            "This is LLM repair planning only. It may propose next steps, but "
            "it does not approve, dispatch, verify runtime progress, or count "
            "toward #640."
        ),
    }


def run_sitl_bounded_dispatch_execution(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    backend_action: str = "operator_gated_recovery_dispatch",
    backend_action_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute an operator-gated SITL dispatch when runtime opt-in is present.

    Without opt-in the chain remains artifact-only. With opt-in it validates the
    runtime dispatch authority table before invoking the PX4/Gazebo smoke path.
    """

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    policy_summary = build_policy_authority_summary(artifact_root=root)
    if policy_summary.get("summary_status") != "authority_runtime_applied":
        return build_sitl_dispatch_execution_summary(artifact_root=root)

    chain = _latest_policy_chain(root)
    policy_path, policy = chain["policy"]
    rule_path, rule = chain["rule"]
    authority_path, authority = chain["authority"]
    generated_at = datetime.now(timezone.utc).isoformat()
    session_id = f"missionos_sitl_dispatch_session_{uuid.uuid4().hex[:12]}"
    execution_dispatch_ref = f"{authority.get('dispatch_ref')}:session:{session_id}"
    runtime_payload: Mapping[str, Any] = {}
    runtime_summary: Mapping[str, Any] = {}
    runtime_dispatch_supported = False
    runtime_fields: dict[str, Any] = {}

    approval_dir = _artifact_dir(root, "operator_dispatch_approval")
    approval_path = approval_dir / "operator_dispatch_approval_record.json"
    approval = {
        "schema_version": OPERATOR_DISPATCH_APPROVAL_SCHEMA_VERSION,
        "approval_id": f"operator_dispatch_approval_{uuid.uuid4().hex[:12]}",
        "approval_status": "approved",
        "generated_at": generated_at,
        "session_id": session_id,
        "bounded_dispatch_authority_ref": f"bounded_dispatch_authority:{authority.get('dispatch_authority_id')}",
        "bounded_dispatch_authority_artifact_path": authority_path,
        "active_policy_ref": authority.get("active_policy_ref"),
        "active_policy_artifact_path": policy_path,
        "recovery_rule_ref": authority.get("automatic_recovery_rule_ref"),
        "recovery_rule_artifact_path": rule_path,
        "operator_approved_in_artifact": True,
        "operator_approval_required": True,
        "dispatch_trigger": "operator_approved",
        "automatic_dispatch_executed": False,
        "approval_free_stronger_execution": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        **runtime_fields,
    }
    approval_dir.mkdir(parents=True, exist_ok=True)
    approval_path.write_text(json.dumps(approval, indent=2, sort_keys=True), encoding="utf-8")

    gate_dir = _artifact_dir(root, "deterministic_dispatch_gate")
    gate_path = gate_dir / "deterministic_dispatch_gate_result.json"
    gate = {
        "schema_version": DETERMINISTIC_DISPATCH_GATE_SCHEMA_VERSION,
        "gate_result_id": f"deterministic_dispatch_gate_{uuid.uuid4().hex[:12]}",
        "gate_status": "passed",
        "generated_at": generated_at,
        "session_id": session_id,
        "deterministic_gate_passed_in_artifact": True,
        "operator_dispatch_approval_ref": f"operator_dispatch_approval:{approval['approval_id']}",
        "operator_dispatch_approval_artifact_path": _relative(approval_path),
        "bounded_dispatch_authority_artifact_path": authority_path,
        "active_policy_artifact_path": policy_path,
        "recovery_rule_artifact_path": rule_path,
        "bounded_action_ref": authority.get("bounded_action_ref"),
        "dispatch_ref": execution_dispatch_ref,
        "operator_approval_required": True,
        "llm_gate_judge_used": False,
        "approval_free_stronger_execution": False,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "blocked_reasons": [],
        **runtime_fields,
    }
    gate_dir.mkdir(parents=True, exist_ok=True)
    gate_path.write_text(json.dumps(gate, indent=2, sort_keys=True), encoding="utf-8")

    authority_table_path = Path(str(authority.get("dispatch_authority_table_state_path") or ""))
    authority_validation = (
        DispatchAuthorityTable(authority_table_path).validate_dispatch_request(
            authority_id=str(authority.get("dispatch_authority_id") or ""),
            operator_approval=approval,
            deterministic_gate=gate,
        )
        if authority_table_path.exists()
        else {
            "schema_version": "missionos_dispatch_authority_validation.v1",
            "validation_status": "blocked",
            "authority_registered": False,
        }
    )
    if authority_validation.get("validation_status") == "valid":
        runtime_payload = invoke_missionos_sitl_dispatch_runtime(
            artifact_root=root,
            backend_action=backend_action,
            backend_action_parameters=backend_action_parameters,
        )
        runtime_evidence = runtime_payload.get("runtime_invocation_evidence")
        runtime_summary = _as_mapping(runtime_payload.get("runtime_summary"))
        runtime_dispatch_supported = bool(
            runtime_payload.get("runtime_invoked") is True
            and _as_mapping(runtime_evidence)
            and _as_mapping(runtime_evidence).get("invocation_exit_code") == 0
            and runtime_summary_supports_dispatch(dict(runtime_summary))
        )
        runtime_fields = (
            {"runtime_invocation_evidence": runtime_evidence}
            if isinstance(runtime_evidence, Mapping)
            else {}
        )

    approval.update(
        {
            "operator_approved_in_runtime": runtime_dispatch_supported,
            "operator_approval_token_consumed_by_authority_table": (
                authority_validation.get("operator_approval_token_consumed") is True
            ),
            "dispatch_replay_detected": authority_validation.get("dispatch_replay_detected") is True,
            "dispatch_authority_validation": authority_validation,
            **runtime_fields,
        }
    )
    approval = _write_artifact(approval_path, approval)
    gate.update(
        {
            "deterministic_gate_passed_in_runtime": runtime_dispatch_supported,
            "gate_result_consumed_by_authority_table": (
                authority_validation.get("gate_result_consumed") is True
            ),
            "dispatch_replay_detected": authority_validation.get("dispatch_replay_detected") is True,
            "dispatch_authority_validation": authority_validation,
            **runtime_fields,
        }
    )
    gate = _write_artifact(gate_path, gate)

    execution_dir = _artifact_dir(root, "bounded_dispatch_execution")
    execution_path = execution_dir / "bounded_dispatch_execution_receipt.json"
    execution = {
        "schema_version": BOUNDED_DISPATCH_EXECUTION_SCHEMA_VERSION,
        "execution_receipt_id": f"bounded_dispatch_execution_{uuid.uuid4().hex[:12]}",
        "execution_status": "executed" if runtime_dispatch_supported else "artifact_materialized",
        "generated_at": generated_at,
        "session_id": session_id,
        "dispatch_executed_in_artifact": True,
        "dispatch_executed_in_runtime": runtime_dispatch_supported,
        "dispatch_trigger": "operator_approved",
        "automatic_dispatch_executed": False,
        "automatic_dispatch_suppressed": True,
        "operator_approval_required": True,
        "operator_dispatch_approval_ref": f"operator_dispatch_approval:{approval['approval_id']}",
        "operator_dispatch_approval_artifact_path": _relative(approval_path),
        "deterministic_dispatch_gate_ref": f"deterministic_dispatch_gate:{gate['gate_result_id']}",
        "deterministic_dispatch_gate_artifact_path": _relative(gate_path),
        "bounded_dispatch_authority_ref": f"bounded_dispatch_authority:{authority.get('dispatch_authority_id')}",
        "bounded_dispatch_authority_artifact_path": authority_path,
        "active_policy_ref": authority.get("active_policy_ref"),
        "active_policy_artifact_path": policy_path,
        "recovery_rule_ref": authority.get("automatic_recovery_rule_ref"),
        "recovery_rule_artifact_path": rule_path,
        "bounded_action_ref": authority.get("bounded_action_ref"),
        "dispatch_ref": execution_dispatch_ref,
        "backend_target": "px4_gazebo_sitl",
        "core_direct_execution_used": False,
        "llm_gate_judge_used": False,
        "approval_free_stronger_execution": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "public_sync_performed": False,
        "drone_physics_affected": runtime_dispatch_supported,
        "runtime_smoke_summary": dict(runtime_summary),
        "runtime_summary_artifact_path": runtime_payload.get("runtime_summary_artifact_path", ""),
        "runtime_summary_source": runtime_payload.get("runtime_summary_source", ""),
        "runtime_summary_source_path": runtime_payload.get("runtime_summary_source_path", ""),
        "runtime_stdout_tail": runtime_payload.get("runtime_stdout_tail", ""),
        "runtime_stderr_tail": runtime_payload.get("runtime_stderr_tail", ""),
        **runtime_fields,
    }
    execution_dir.mkdir(parents=True, exist_ok=True)
    execution = _write_artifact(execution_path, execution)

    request_dir = _artifact_dir(root, "missionos_backend_action_request")
    request_path = request_dir / "missionos_backend_action_request.json"
    request_ref = f"missionos_backend_action_request:{uuid.uuid4().hex[:12]}"
    request = {
        "schema_version": BACKEND_ACTION_REQUEST_SCHEMA_VERSION,
        "request_ref": request_ref,
        "request_status": "runtime_invoked" if runtime_dispatch_supported else "artifact_materialized",
        "generated_at": generated_at,
        "session_id": session_id,
        "backend_target": "px4_gazebo_sitl",
        "backend_action": backend_action,
        "bounded_action_ref": authority.get("bounded_action_ref"),
        "dispatch_ref": execution_dispatch_ref,
        "execution_receipt_ref": f"bounded_dispatch_execution:{execution['execution_receipt_id']}",
        "execution_receipt_artifact_path": _relative(execution_path),
        "operator_dispatch_approval_artifact_path": _relative(approval_path),
        "deterministic_dispatch_gate_artifact_path": _relative(gate_path),
        "dispatch_executed_in_artifact": True,
        "dispatch_executed_in_runtime": runtime_dispatch_supported,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "drone_physics_affected": runtime_dispatch_supported,
        **runtime_fields,
    }
    request_dir.mkdir(parents=True, exist_ok=True)
    request = _write_artifact(request_path, request)

    outcome_dir = _artifact_dir(root, "missionos_dispatch_outcome_observation")
    outcome_path = outcome_dir / "missionos_dispatch_outcome_observation.json"
    outcome_ref = f"missionos_dispatch_outcome_observation:{uuid.uuid4().hex[:12]}"
    outcome = {
        "schema_version": DISPATCH_OUTCOME_OBSERVATION_SCHEMA_VERSION,
        "outcome_observation_ref": outcome_ref,
        "outcome_status": "observed" if runtime_dispatch_supported else "artifact_materialized",
        "generated_at": generated_at,
        "session_id": session_id,
        "backend_action_request_ref": request_ref,
        "backend_action_request_artifact_path": _relative(request_path),
        "dispatch_ref": execution_dispatch_ref,
        "dispatch_executed_in_artifact": True,
        "dispatch_executed_in_runtime": runtime_dispatch_supported,
        "dispatch_observed_in_artifact": True,
        "outcome_observed_in_artifact": True,
        "outcome_observed_in_runtime": runtime_dispatch_supported,
        "outcome_kind": "sitl_runtime_smoke_summary_observed"
        if runtime_dispatch_supported
        else "sitl_backend_action_request_artifact_materialized",
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "drone_physics_affected": runtime_dispatch_supported,
        **runtime_fields,
    }
    outcome_dir.mkdir(parents=True, exist_ok=True)
    outcome = _write_artifact(outcome_path, outcome)

    verifier_runtime_check = _independent_runtime_dispatch_check(
        execution=execution,
        outcome=outcome,
    )
    verifier_runtime_supported = verifier_runtime_check.get("verifier_passed") is True

    verifier_dir = _artifact_dir(root, "missionos_recovery_verifier")
    verifier_path = verifier_dir / "missionos_recovery_verifier_result.json"
    verifier = {
        "schema_version": RECOVERY_VERIFIER_SCHEMA_VERSION,
        "verifier_result_id": f"missionos_recovery_verifier_{uuid.uuid4().hex[:12]}",
        "verifier_status": "passed" if verifier_runtime_supported else "artifact_only",
        "generated_at": generated_at,
        "session_id": session_id,
        "verified_dispatch_execution_in_artifact": True,
        "verified_dispatch_execution_in_runtime": verifier_runtime_supported,
        "outcome_observed_in_artifact": True,
        "outcome_observed_in_runtime": runtime_dispatch_supported,
        "backend_action_request_ref": request_ref,
        "backend_action_request_artifact_path": _relative(request_path),
        "outcome_observation_ref": outcome_ref,
        "outcome_observation_artifact_path": _relative(outcome_path),
        "dispatch_ref": execution_dispatch_ref,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "blocked_reasons": [],
        "drone_physics_affected": verifier_runtime_supported,
        "verifier_runtime_check_evidence": verifier_runtime_check,
        **runtime_fields,
    }
    verifier_dir.mkdir(parents=True, exist_ok=True)
    verifier = _write_artifact(verifier_path, verifier)

    audit_dir = _artifact_dir(root, "bounded_dispatch_audit")
    audit_path = audit_dir / "bounded_dispatch_audit_record.json"
    audit = {
        "schema_version": DISPATCH_AUDIT_RECORD_SCHEMA_VERSION,
        "audit_record_id": f"bounded_dispatch_audit_{uuid.uuid4().hex[:12]}",
        "audit_status": "recorded",
        "generated_at": generated_at,
        "session_id": session_id,
        "operator_dispatch_approval_artifact_path": _relative(approval_path),
        "deterministic_dispatch_gate_artifact_path": _relative(gate_path),
        "execution_receipt_artifact_path": _relative(execution_path),
        "backend_action_request_artifact_path": _relative(request_path),
        "outcome_observation_artifact_path": _relative(outcome_path),
        "verifier_result_artifact_path": _relative(verifier_path),
        "dispatch_executed_in_artifact": True,
        "dispatch_executed_in_runtime": runtime_dispatch_supported,
        "automatic_dispatch_executed": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "delivery_completion_claimed": False,
        "drone_physics_affected": runtime_dispatch_supported,
        "progress_counted": runtime_dispatch_supported,
        **runtime_fields,
    }
    audit_dir.mkdir(parents=True, exist_ok=True)
    _write_artifact(audit_path, audit)

    return build_sitl_dispatch_execution_summary(artifact_root=root)


def _latest_dispatch_execution_chain(root: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        "approval": (_latest_payloads(root, "operator_dispatch_approval_record.json") or [("", {})])[0],
        "gate": (_latest_payloads(root, "deterministic_dispatch_gate_result.json") or [("", {})])[0],
        "execution": (_latest_payloads(root, "bounded_dispatch_execution_receipt.json") or [("", {})])[0],
        "request": (_latest_payloads(root, "missionos_backend_action_request.json") or [("", {})])[0],
        "outcome": (_latest_payloads(root, "missionos_dispatch_outcome_observation.json") or [("", {})])[0],
        "verifier": (_latest_payloads(root, "missionos_recovery_verifier_result.json") or [("", {})])[0],
        "audit": (_latest_payloads(root, "bounded_dispatch_audit_record.json") or [("", {})])[0],
    }


def build_sitl_dispatch_execution_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Summarize the operator-approved SITL dispatch execution chain."""

    root = Path(artifact_root)
    policy_summary = build_policy_authority_summary(artifact_root=root)
    execution_chain = _latest_dispatch_execution_chain(root)
    approval_path, approval = execution_chain["approval"]
    gate_path, gate = execution_chain["gate"]
    execution_path, execution = execution_chain["execution"]
    request_path, request = execution_chain["request"]
    outcome_path, outcome = execution_chain["outcome"]
    verifier_path, verifier = execution_chain["verifier"]
    audit_path, audit = execution_chain["audit"]
    approval, approval_runtime_blocks = _runtime_claims(approval)
    gate, gate_runtime_blocks = _runtime_claims(gate)
    execution, execution_runtime_blocks = _runtime_claims(execution)
    request, request_runtime_blocks = _runtime_claims(request)
    outcome, outcome_runtime_blocks = _runtime_claims(outcome)
    verifier, verifier_runtime_blocks = _runtime_claims(verifier)
    audit, audit_runtime_blocks = _runtime_claims(audit)
    paths = (approval_path, gate_path, execution_path, request_path, outcome_path, verifier_path, audit_path)
    same_session = bool(
        approval.get("session_id")
        and len({
            approval.get("session_id"),
            gate.get("session_id"),
            execution.get("session_id"),
            request.get("session_id"),
            outcome.get("session_id"),
            verifier.get("session_id"),
            audit.get("session_id"),
        }) == 1
    )
    refs_consistent = bool(
        same_session
        and gate.get("operator_dispatch_approval_artifact_path") == approval_path
        and execution.get("operator_dispatch_approval_artifact_path") == approval_path
        and execution.get("deterministic_dispatch_gate_artifact_path") == gate_path
        and request.get("execution_receipt_artifact_path") == execution_path
        and outcome.get("backend_action_request_artifact_path") == request_path
        and verifier.get("backend_action_request_artifact_path") == request_path
        and verifier.get("outcome_observation_artifact_path") == outcome_path
        and audit.get("execution_receipt_artifact_path") == execution_path
        and audit.get("verifier_result_artifact_path") == verifier_path
    )
    forbidden_true = [
        key
        for key in (
            "automatic_dispatch_executed",
            "physical_execution_invoked",
            "hardware_target_allowed",
            "core_direct_execution_used",
            "llm_gate_judge_used",
            "approval_free_stronger_execution",
            "delivery_completion_claimed",
            "public_sync_performed",
        )
        if approval.get(key) is True
        or gate.get(key) is True
        or execution.get(key) is True
        or request.get(key) is True
        or outcome.get(key) is True
        or verifier.get(key) is True
        or audit.get(key) is True
    ]
    required_true = {
        "policy_authority_runtime_applied": policy_summary.get("summary_status") == "authority_runtime_applied",
        "operator_approval_recorded_in_artifact": approval.get("operator_approved_in_artifact") is True,
        "deterministic_gate_recorded_in_artifact": gate.get("deterministic_gate_passed_in_artifact") is True,
        "dispatch_recorded_in_artifact": execution.get("dispatch_executed_in_artifact") is True,
        "backend_action_request_materialized": request.get("request_status")
        in {"artifact_materialized", "runtime_invoked"},
        "outcome_recorded_in_artifact": outcome.get("outcome_observed_in_artifact") is True,
        "verifier_recorded_in_artifact": verifier.get("verified_dispatch_execution_in_artifact") is True,
    }
    blocking_reasons = []
    if any(path == "" for path in paths):
        blocking_reasons.append("dispatch_execution_chain_incomplete")
    missing_required = [key for key, value in required_true.items() if not value]
    blocking_reasons.extend(f"{key}_not_observed" for key in missing_required)
    if not same_session:
        blocking_reasons.append("dispatch_execution_same_session_not_observed")
    if not refs_consistent:
        blocking_reasons.append("dispatch_execution_ref_chain_mismatch")
    blocking_reasons.extend(forbidden_true)
    runtime_blocking_reasons = (
        approval_runtime_blocks
        + gate_runtime_blocks
        + execution_runtime_blocks
        + request_runtime_blocks
        + outcome_runtime_blocks
        + verifier_runtime_blocks
        + audit_runtime_blocks
    )
    blocking_reasons.extend(runtime_blocking_reasons)
    runtime_dispatch_observed = bool(
        execution.get("dispatch_executed_in_runtime") is True
        and outcome.get("outcome_observed_in_runtime") is True
        and verifier.get("verified_dispatch_execution_in_runtime") is True
    )
    summary_status = (
        "executed_observed"
        if not blocking_reasons and runtime_dispatch_observed
        else "artifact_only_observed"
        if not blocking_reasons
        else "blocked"
        if any(paths)
        else "missing"
    )
    return {
        "schema_version": SITL_DISPATCH_EXECUTION_SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_status": summary_status,
        "classification": {
            "causal_form": (
                "Form 2a execution evidence"
                if runtime_dispatch_observed
                else "Form 2a candidate (artifact-only)"
            ),
            "surface": "operator-approved bounded SITL dispatch artifact chain",
            "progress_counted": runtime_dispatch_observed,
            "full_form3_closed_loop": False,
            "drone_physics_affected": runtime_dispatch_observed,
        },
        "operator_dispatch_approval": {
            "status": approval.get("approval_status") or "missing",
            "artifact_path": approval_path,
            "operator_approved": approval.get("operator_approved_in_runtime") is True,
            "operator_approved_in_artifact": approval.get("operator_approved_in_artifact") is True,
            "operator_approved_in_runtime": approval.get("operator_approved_in_runtime") is True,
        },
        "deterministic_dispatch_gate": {
            "status": gate.get("gate_status") or "missing",
            "artifact_path": gate_path,
            "deterministic_gate_passed": gate.get("deterministic_gate_passed_in_runtime") is True,
            "deterministic_gate_passed_in_artifact": gate.get("deterministic_gate_passed_in_artifact") is True,
            "deterministic_gate_passed_in_runtime": gate.get("deterministic_gate_passed_in_runtime") is True,
        },
        "bounded_dispatch_execution": {
            "status": execution.get("execution_status") or "missing",
            "artifact_path": execution_path,
            "dispatch_executed": execution.get("dispatch_executed_in_runtime") is True,
            "dispatch_executed_in_artifact": execution.get("dispatch_executed_in_artifact") is True,
            "dispatch_executed_in_runtime": execution.get("dispatch_executed_in_runtime") is True,
            "dispatch_trigger": execution.get("dispatch_trigger"),
        },
        "backend_action_request": {
            "status": request.get("request_status") or "missing",
            "artifact_path": request_path,
            "backend_target": request.get("backend_target"),
        },
        "dispatch_outcome_observation": {
            "status": outcome.get("outcome_status") or "missing",
            "artifact_path": outcome_path,
            "outcome_observed": outcome.get("outcome_observed_in_runtime") is True,
            "outcome_observed_in_artifact": outcome.get("outcome_observed_in_artifact") is True,
            "outcome_observed_in_runtime": outcome.get("outcome_observed_in_runtime") is True,
        },
        "recovery_verifier_result": {
            "status": verifier.get("verifier_status") or "missing",
            "artifact_path": verifier_path,
            "verified_dispatch_execution": verifier.get("verified_dispatch_execution_in_runtime") is True,
            "verified_dispatch_execution_in_artifact": verifier.get(
                "verified_dispatch_execution_in_artifact"
            )
            is True,
            "verified_dispatch_execution_in_runtime": verifier.get(
                "verified_dispatch_execution_in_runtime"
            )
            is True,
        },
        "audit_record": {
            "status": audit.get("audit_status") or "missing",
            "artifact_path": audit_path,
        },
        "authority_boundary": {
            "dispatch_executed": execution.get("dispatch_executed_in_runtime") is True,
            "dispatch_executed_in_artifact": execution.get("dispatch_executed_in_artifact") is True,
            "dispatch_executed_in_runtime": execution.get("dispatch_executed_in_runtime") is True,
            "dispatch_trigger": execution.get("dispatch_trigger"),
            "automatic_dispatch_executed": execution.get("automatic_dispatch_executed") is True,
            "operator_approved_in_artifact": approval.get("operator_approved_in_artifact") is True,
            "operator_approved_in_runtime": approval.get("operator_approved_in_runtime") is True,
            "deterministic_gate_passed_in_artifact": gate.get("deterministic_gate_passed_in_artifact") is True,
            "deterministic_gate_passed_in_runtime": gate.get("deterministic_gate_passed_in_runtime") is True,
            "outcome_observed_in_artifact": outcome.get("outcome_observed_in_artifact") is True,
            "outcome_observed_in_runtime": outcome.get("outcome_observed_in_runtime") is True,
            "verified_dispatch_execution_in_artifact": verifier.get(
                "verified_dispatch_execution_in_artifact"
            )
            is True,
            "verified_dispatch_execution_in_runtime": verifier.get(
                "verified_dispatch_execution_in_runtime"
            )
            is True,
            "operator_approval_required": execution.get("operator_approval_required") is True,
            "automatic_dispatch_suppressed": execution.get("automatic_dispatch_suppressed") is True,
            "physical_execution_invoked": execution.get("physical_execution_invoked") is True,
            "hardware_target_allowed": execution.get("hardware_target_allowed") is True,
            "core_direct_execution_used": execution.get("core_direct_execution_used") is True,
            "llm_gate_judge_used": execution.get("llm_gate_judge_used") is True,
            "approval_free_stronger_execution": execution.get("approval_free_stronger_execution") is True,
            "delivery_completion_claimed": execution.get("delivery_completion_claimed") is True,
            "same_session": same_session,
            "refs_consistent": refs_consistent,
            "runtime_invocation_evidence_present": execution.get("runtime_invocation_evidence") is not None,
            "runtime_dispatch_observed": runtime_dispatch_observed,
            "drone_physics_affected": runtime_dispatch_observed,
            "runtime_claim_validation": {
                "approval": runtime_claim_validation_summary(approval),
                "gate": runtime_claim_validation_summary(gate),
                "execution": runtime_claim_validation_summary(execution),
                "outcome": runtime_claim_validation_summary(outcome),
                "verifier": runtime_claim_validation_summary(verifier),
            },
            "blocking_reasons": blocking_reasons,
        },
        "operator_note": (
            "This dispatch was executed through the PX4/Gazebo horizontal route "
            "smoke subprocess and carries runtime invocation evidence. It is "
            "still not automatic dispatch, hardware authority, physical "
            "execution, delivery completion, or full Form 3."
            if runtime_dispatch_observed
            else "This is an operator-approved SITL dispatch artifact chain only. "
            "No successful PX4/Gazebo/MAVLink/gz/docker/subprocess runtime "
            "invocation evidence is attached, so dispatch, outcome, and "
            "verifier claims remain false in runtime and do not count as progress."
        ),
    }


def select_cycle2_response(
    cycle1_outcome: Mapping[str, Any],
    cycle1_verifier: Mapping[str, Any],
) -> str:
    """Derive the second response from the first runtime dispatch outcome."""

    if (
        cycle1_outcome.get("outcome_observed_in_runtime") is True
        and cycle1_verifier.get("verified_dispatch_execution_in_runtime") is True
    ):
        return "operator_gated_followup_recovery_dispatch"
    if (
        cycle1_outcome.get("outcome_observed_in_runtime") is True
        and cycle1_verifier.get("verified_dispatch_execution_in_runtime") is not True
    ):
        return "operator_gated_recovery_repeat_with_verifier_repair"
    raise RuntimeError("cycle1_outcome_not_observed")


def _dispatch_ref_from_summary(root: Path, summary: Mapping[str, Any]) -> str:
    artifact_path = str(_as_mapping(summary.get("bounded_dispatch_execution")).get("artifact_path") or "")
    execution = _read_json(_resolve_artifact_path(root, artifact_path)) if artifact_path else {}
    return str(_as_mapping(execution).get("dispatch_ref") or "")


def _runtime_evidence_present_from_summary(root: Path, summary: Mapping[str, Any]) -> bool:
    artifact_path = str(_as_mapping(summary.get("bounded_dispatch_execution")).get("artifact_path") or "")
    execution = _read_json(_resolve_artifact_path(root, artifact_path)) if artifact_path else {}
    return isinstance(_as_mapping(execution).get("runtime_invocation_evidence"), Mapping)


def run_scoped_form3_closed_loop(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Execute a scoped two-cycle MissionOS runtime closed loop."""

    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    cycle1 = run_sitl_bounded_dispatch_execution(artifact_root=root)
    if cycle1.get("summary_status") != "executed_observed":
        return build_scoped_form3_closed_loop_summary(artifact_root=root)

    cycle1_outcome_path = str(
        _as_mapping(cycle1.get("dispatch_outcome_observation")).get("artifact_path") or ""
    )
    cycle1_verifier_path = str(
        _as_mapping(cycle1.get("recovery_verifier_result")).get("artifact_path") or ""
    )
    cycle1_outcome = _read_json(_resolve_artifact_path(root, cycle1_outcome_path)) or {}
    cycle1_verifier = _read_json(_resolve_artifact_path(root, cycle1_verifier_path)) or {}
    try:
        cycle2_response = select_cycle2_response(cycle1_outcome, cycle1_verifier)
    except RuntimeError:
        return build_scoped_form3_closed_loop_summary(artifact_root=root)

    generated_at = datetime.now(timezone.utc).isoformat()
    condition_dir = _artifact_dir(root, "scoped_form3_condition_derivation")
    condition_path = condition_dir / "condition_derivation_evidence.json"
    condition = {
        "schema_version": "missionos_scoped_form3_condition_derivation_evidence.v1",
        "generated_at": generated_at,
        "cycle1_outcome_observation_artifact_path": cycle1_outcome_path,
        "cycle1_verifier_artifact_path": cycle1_verifier_path,
        "cycle1_outcome_observed_in_runtime": cycle1_outcome.get("outcome_observed_in_runtime") is True,
        "cycle1_verified_dispatch_execution_in_runtime": cycle1_verifier.get(
            "verified_dispatch_execution_in_runtime"
        )
        is True,
        "cycle2_response": cycle2_response,
        "cycle2_response_derived_from_cycle1_outcome": True,
        "cycle2_condition": "followup_recovery_after_runtime_observed_dispatch",
    }
    condition_dir.mkdir(parents=True, exist_ok=True)
    _write_artifact(condition_path, condition)

    cycle2 = run_sitl_bounded_dispatch_execution(
        artifact_root=root,
        backend_action=cycle2_response,
    )
    cycle1_dispatch_ref = _dispatch_ref_from_summary(root, cycle1)
    cycle2_dispatch_ref = _dispatch_ref_from_summary(root, cycle2)
    cycle1_evidence = _runtime_evidence_present_from_summary(root, cycle1)
    cycle2_evidence = _runtime_evidence_present_from_summary(root, cycle2)
    form3_passed = bool(
        cycle2.get("summary_status") == "executed_observed"
        and cycle1_dispatch_ref
        and cycle2_dispatch_ref
        and cycle1_dispatch_ref != cycle2_dispatch_ref
        and cycle1_evidence
        and cycle2_evidence
    )
    record_dir = _artifact_dir(root, "scoped_form3_closed_loop")
    record_path = record_dir / "scoped_form3_closed_loop_record.json"
    record = {
        "schema_version": SCOPED_FORM3_CLOSED_LOOP_RECORD_SCHEMA_VERSION,
        "closed_loop_record_id": f"scoped_form3_closed_loop_{uuid.uuid4().hex[:12]}",
        "record_status": "closed_loop_runtime_observed" if form3_passed else "blocked",
        "generated_at": generated_at,
        "closed_loop_cycle_count": 2,
        "same_session_evidence": True,
        "cycle1_dispatch_summary": cycle1,
        "cycle2_dispatch_summary": cycle2,
        "cycle1_dispatch_ref": cycle1_dispatch_ref,
        "cycle2_dispatch_ref": cycle2_dispatch_ref,
        "cycle1_runtime_invocation_evidence_present": cycle1_evidence,
        "cycle2_runtime_invocation_evidence_present": cycle2_evidence,
        "cycle2_response": cycle2_response,
        "condition_derivation_evidence_artifact_path": _relative(condition_path),
        "cycle2_response_derived_from_cycle1_outcome": True,
        "cycle2_reobservation_from_cycle2_runtime_outcome": cycle2.get("summary_status")
        == "executed_observed",
        "automatic_dispatch_executed": False,
        "dispatch_trigger": "operator_approved",
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "llm_gate_judge_used": False,
        "delivery_completion_claimed": False,
        "progress_counted": form3_passed,
    }
    record_dir.mkdir(parents=True, exist_ok=True)
    _write_artifact(record_path, record)
    return build_scoped_form3_closed_loop_summary(artifact_root=root)


def _latest_scoped_form3_record(root: Path) -> tuple[str, dict[str, Any]]:
    records = _latest_payloads(root, "scoped_form3_closed_loop_record.json")
    return records[0] if records else ("", {})


def build_scoped_form3_closed_loop_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Summarize the scoped two-cycle MissionOS closed loop."""

    root = Path(artifact_root)
    record_path, record = _latest_scoped_form3_record(root)
    cycle1 = _as_mapping(record.get("cycle1_dispatch_summary"))
    cycle2 = _as_mapping(record.get("cycle2_dispatch_summary"))
    closed_loop_observed = bool(
        record.get("record_status") == "closed_loop_runtime_observed"
        and record.get("closed_loop_cycle_count") == 2
        and record.get("cycle1_runtime_invocation_evidence_present") is True
        and record.get("cycle2_runtime_invocation_evidence_present") is True
        and record.get("cycle1_dispatch_ref")
        and record.get("cycle2_dispatch_ref")
        and record.get("cycle1_dispatch_ref") != record.get("cycle2_dispatch_ref")
        and record.get("cycle2_response_derived_from_cycle1_outcome") is True
        and record.get("cycle2_reobservation_from_cycle2_runtime_outcome") is True
    )
    return {
        "schema_version": SCOPED_FORM3_CLOSED_LOOP_SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary_status": "closed_loop_runtime_observed" if closed_loop_observed else "missing",
        "classification": {
            "causal_form": "Form 3" if closed_loop_observed else "Form 3 blocked",
            "surface": "two-cycle operator-approved PX4/Gazebo SITL closed loop",
            "progress_counted": closed_loop_observed,
            "full_form3_closed_loop": closed_loop_observed,
            "drone_physics_affected": closed_loop_observed,
        },
        "closed_loop_record": {
            "status": record.get("record_status") or "missing",
            "artifact_path": record_path,
            "closed_loop_cycle_count": record.get("closed_loop_cycle_count") or 0,
            "same_session_evidence": record.get("same_session_evidence") is True,
        },
        "cycle1": {
            "summary_status": cycle1.get("summary_status") or "missing",
            "dispatch_ref": record.get("cycle1_dispatch_ref"),
            "runtime_invocation_evidence_present": record.get(
                "cycle1_runtime_invocation_evidence_present"
            )
            is True,
        },
        "cycle2": {
            "summary_status": cycle2.get("summary_status") or "missing",
            "dispatch_ref": record.get("cycle2_dispatch_ref"),
            "runtime_invocation_evidence_present": record.get(
                "cycle2_runtime_invocation_evidence_present"
            )
            is True,
            "response": record.get("cycle2_response"),
            "response_derived_from_cycle1_outcome": record.get(
                "cycle2_response_derived_from_cycle1_outcome"
            )
            is True,
            "reobservation_from_runtime_outcome": record.get(
                "cycle2_reobservation_from_cycle2_runtime_outcome"
            )
            is True,
        },
        "authority_boundary": {
            "closed_loop_cycle_count": record.get("closed_loop_cycle_count") or 0,
            "cycle1_dispatch_ref": record.get("cycle1_dispatch_ref"),
            "cycle2_dispatch_ref": record.get("cycle2_dispatch_ref"),
            "cycle1_dispatch_ref_distinct_from_cycle2": bool(
                record.get("cycle1_dispatch_ref")
                and record.get("cycle2_dispatch_ref")
                and record.get("cycle1_dispatch_ref") != record.get("cycle2_dispatch_ref")
            ),
            "cycle1_runtime_invocation_evidence_present": record.get(
                "cycle1_runtime_invocation_evidence_present"
            )
            is True,
            "cycle2_runtime_invocation_evidence_present": record.get(
                "cycle2_runtime_invocation_evidence_present"
            )
            is True,
            "cycle2_response_derived_from_cycle1_outcome": record.get(
                "cycle2_response_derived_from_cycle1_outcome"
            )
            is True,
            "cycle2_reobservation_from_cycle2_runtime_outcome": record.get(
                "cycle2_reobservation_from_cycle2_runtime_outcome"
            )
            is True,
            "automatic_dispatch_executed": record.get("automatic_dispatch_executed") is True,
            "physical_execution_invoked": record.get("physical_execution_invoked") is True,
            "hardware_target_allowed": record.get("hardware_target_allowed") is True,
            "llm_gate_judge_used": record.get("llm_gate_judge_used") is True,
            "delivery_completion_claimed": record.get("delivery_completion_claimed") is True,
            "form3_runtime_observed": closed_loop_observed,
        },
        "operator_note": (
            "This scoped Form 3 run executed two distinct operator-approved SITL "
            "runtime dispatch cycles and derived the second response from the "
            "first runtime outcome."
            if closed_loop_observed
            else "No two-cycle runtime closed loop is currently observed."
        ),
    }


def build_missionos_knowledge_sharing_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Return a read-only summary of L3/L4 knowledge sharing artifacts."""

    root = Path(artifact_root)
    knowledge = build_missionos_knowledge_browser_summary(artifact_root=root)
    current_source = _choose_source_card(knowledge)
    current_source_persistable = _source_card_persistable(current_source)
    lessons = _latest_payloads(root, "cross_session_lesson.json")
    curator_runs = _latest_payloads(root, "knowledge_curator_dry_run.json")
    production_curator_runs = _latest_payloads(root, "knowledge_curator_run.json")
    active_indexes = _latest_payloads(root, "active_lesson_index.json")
    latest_lesson_path, latest_lesson = lessons[0] if lessons else ("", {})
    latest_dry_run_path, latest_dry_run = curator_runs[0] if curator_runs else ("", {})
    latest_production_path, latest_production_curator = (
        production_curator_runs[0] if production_curator_runs else ("", {})
    )
    latest_index_path, latest_index = active_indexes[0] if active_indexes else ("", {})
    latest_production_curator, production_runtime_blocks = _runtime_claims(latest_production_curator)
    latest_index, index_runtime_blocks = _runtime_claims(latest_index)
    production_curator_completed = latest_production_curator.get("curator_status") == "completed"
    latest_curator_path, latest_curator = (
        (latest_production_path, latest_production_curator)
        if production_curator_completed
        else (latest_dry_run_path, latest_dry_run)
    )
    production_reflected = (
        production_curator_completed
        and latest_index.get("index_status") == "updated"
        and latest_production_curator.get("knowledge_index_updated_in_runtime") is True
        and latest_index.get("knowledge_index_updated_in_runtime") is True
    )
    lesson_persisted = latest_lesson.get("lesson_status") in {
        "persisted_candidate",
        "active_diagnostic_lesson",
    }
    dry_run_started = latest_curator.get("dry_run_agent_execution_started") is True
    dry_run_only = latest_curator.get("dry_run_only") is True
    no_background_automation = latest_curator.get("no_background_automation") is True
    agent_execution_started = latest_curator.get("agent_execution_started_in_runtime") is True
    operator_approved = latest_curator.get("operator_approved_in_runtime") is True
    expected_lesson_ref = (
        f"cross_session_lesson:{latest_lesson.get('lesson_id')}"
        if latest_lesson.get("lesson_id")
        else ""
    )
    lesson_curator_ref_consistent = bool(
        lesson_persisted
        and (latest_curator.get("curator_status") == "completed")
        and latest_curator.get("cross_session_lesson_ref") == expected_lesson_ref
        and latest_curator.get("cross_session_lesson_artifact_path") == latest_lesson_path
    )
    index_ref_consistent = bool(
        production_reflected
        and latest_index.get("lesson_ref") == expected_lesson_ref
        and latest_index.get("lesson_artifact_path") == latest_lesson_path
        and latest_curator.get("active_lesson_index_artifact_path") == latest_index_path
    )
    source_bound_current = bool(
        lesson_persisted
        and current_source_persistable
        and latest_lesson.get("source_failure_mode_id") == current_source.get("failure_mode_id")
        and latest_lesson.get("source_artifact_path") == current_source.get("artifact_path")
    )
    agent_execution_allowed = bool(
        production_reflected
        and operator_approved
        and agent_execution_started
        and no_background_automation
        and latest_curator.get("background_work_scheduled") is not True
    )
    forbidden_true = [
        key
        for key in (
            "policy_update_applied",
            "automatic_recovery_rule_created",
            "dispatch_authority_created",
            "delivery_completion_claimed",
            "physical_execution_invoked",
            "hardware_target_allowed",
            "public_sync_performed",
            "background_work_scheduled",
        )
        if latest_lesson.get(key) is True
        or latest_curator.get(key) is True
        or latest_index.get(key) is True
    ]
    if agent_execution_started and not agent_execution_allowed:
        forbidden_true.append("agent_execution_started")
    blocking_reasons = []
    blocking_reasons.extend(production_runtime_blocks)
    blocking_reasons.extend(index_runtime_blocks)
    if forbidden_true:
        blocking_reasons.extend(forbidden_true)
    if (lesson_persisted or latest_curator.get("curator_status") == "completed") and not current_source_persistable:
        blocking_reasons.append("current_source_not_persistable")
    if (lesson_persisted or latest_curator.get("curator_status") == "completed") and not lesson_curator_ref_consistent:
        blocking_reasons.append("lesson_curator_ref_mismatch")
    if production_reflected and not index_ref_consistent:
        blocking_reasons.append("active_lesson_index_ref_mismatch")
    if (lesson_persisted or latest_curator.get("curator_status") == "completed") and current_source_persistable and not source_bound_current:
        blocking_reasons.append("current_source_ref_mismatch")
    summary_status = (
        "blocked"
        if blocking_reasons
        else "production_reflected"
        if (
            lesson_persisted
            and production_reflected
            and agent_execution_allowed
            and lesson_curator_ref_consistent
            and index_ref_consistent
            and source_bound_current
        )
        else "observed"
        if (
            lesson_persisted
            and latest_dry_run.get("curator_status") == "completed"
            and dry_run_started
            and dry_run_only
            and no_background_automation
            and lesson_curator_ref_consistent
            and source_bound_current
        )
        else "missing"
    )
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_root": _relative(root),
        "surface_label": (
            "Knowledge Sharing Success - Production Reflected"
            if summary_status == "production_reflected"
            else "Knowledge Sharing Success - Dry Run"
        ),
        "summary_status": summary_status,
        "classification": {
            "causal_form": "Form 0b",
            "surface": "L3/L4 knowledge sharing visualization",
            "progress_counted": False,
            "runtime_capability_added": False,
        },
        "l3_cross_session_lesson": {
            "status": "persisted" if lesson_persisted else "missing",
            "lesson_id": latest_lesson.get("lesson_id"),
            "source_failure_mode_id": latest_lesson.get("source_failure_mode_id"),
            "source_artifact_path": latest_lesson.get("source_artifact_path"),
            "artifact_path": latest_lesson_path,
            "reuse_scope": latest_lesson.get("reuse_scope"),
            "production_reflected": latest_lesson.get("production_reflected") is True,
        },
        "current_source": {
            "status": current_source.get("status") or "missing",
            "failure_mode_id": current_source.get("failure_mode_id"),
            "artifact_path": current_source.get("artifact_path"),
            "persistable": current_source_persistable,
        },
        "l4_knowledge_curator": {
            "status": (
                "production_completed"
                if production_reflected
                else "dry_run_completed"
                if latest_dry_run.get("curator_status") == "completed"
                else "not_run"
            ),
            "curator_run_id": latest_curator.get("curator_run_id"),
            "dry_run_only": dry_run_only,
            "dry_run_agent_execution_started": dry_run_started,
            "operator_approved": operator_approved,
            "agent_execution_started": agent_execution_started,
            "no_background_automation": no_background_automation,
            "background_work_scheduled": latest_curator.get("background_work_scheduled") is True,
            "knowledge_index_updated": latest_curator.get("knowledge_index_updated_in_runtime") is True,
            "agent_runtime_ref": latest_curator.get("agent_runtime_ref"),
            "worker_process_pid": latest_curator.get("worker_process_pid"),
            "cross_session_lesson_artifact_path": latest_curator.get(
                "cross_session_lesson_artifact_path"
            ),
            "active_lesson_index_artifact_path": latest_curator.get(
                "active_lesson_index_artifact_path"
            ),
            "artifact_path": latest_curator_path,
        },
        "active_lesson_index": {
            "status": "updated" if latest_index.get("index_status") == "updated" else "missing",
            "index_id": latest_index.get("index_id"),
            "lesson_artifact_path": latest_index.get("lesson_artifact_path"),
            "artifact_path": latest_index_path,
            "active_for_future_diagnostics": latest_index.get("active_for_future_diagnostics") is True,
        },
        "authority_boundary": {
            "agent_execution_started": agent_execution_started,
            "agent_execution_allowed": agent_execution_allowed,
            "operator_approved": operator_approved,
            "dry_run_only": dry_run_only,
            "dry_run_agent_execution_started": dry_run_started,
            "no_background_automation": no_background_automation,
            "background_work_scheduled": latest_curator.get("background_work_scheduled") is True,
            "knowledge_index_updated": latest_curator.get("knowledge_index_updated_in_runtime") is True,
            "production_reflected": production_reflected,
            "policy_update_applied": latest_curator.get("policy_update_applied") is True,
            "automatic_recovery_rule_created": latest_curator.get("automatic_recovery_rule_created") is True,
            "dispatch_authority_created": latest_curator.get("dispatch_authority_created") is True,
            "delivery_completion_claimed": latest_curator.get("delivery_completion_claimed") is True,
            "physical_execution_invoked": latest_curator.get("physical_execution_invoked") is True,
            "hardware_target_allowed": latest_curator.get("hardware_target_allowed") is True,
            "public_sync_performed": latest_curator.get("public_sync_performed") is True,
            "current_source_persistable": current_source_persistable,
            "source_bound_current": source_bound_current,
            "lesson_curator_ref_consistent": lesson_curator_ref_consistent,
            "active_lesson_index_ref_consistent": index_ref_consistent,
            "authority_boundary_supported": not blocking_reasons,
            "authority_true_flags": forbidden_true,
            "blocking_reasons": blocking_reasons,
        },
        "operator_note": (
            "A diagnostic cross-session lesson was reflected into the active "
            "knowledge index by an operator-approved Knowledge Curator run. "
            "This is production knowledge sharing only: no policy, automatic "
            "recovery rule, dispatch, physical execution, delivery completion, "
            "or public sync is claimed."
            if summary_status == "production_reflected"
            else "This is the first safe success condition for L3/L4: a diagnostic "
            "cross-session lesson was persisted and the Knowledge Curator dry-run "
            "recorded that it selected it. No policy, recovery rule, dispatch, "
            "agent startup, physical execution, or delivery completion is claimed."
        ),
        "not_claimed": [
            "policy_update",
            "automatic_recovery_rule",
            "dispatch_authority",
            "physical_execution",
            "delivery_completion",
            "public_sync",
        ],
    }
