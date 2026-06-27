"""Read-only MissionOS knowledge / failure-mode browser projection.

The browser indexes persisted diagnostic artifacts and turns blocked / partial
runtime facts into operator-readable cards. It must not start SITL, invoke
Gateway probes, promote lessons into policy, create dispatch authority, or
mutate mission state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from src.gateway.missionos_milestone import (
    ARTIFACT_ROOT,
    AUTHORITY_FALSE_KEYS,
    _authority_false_summary,
    _positive_evidence_path,
    _relative,
)


SCHEMA_VERSION = "missionos_knowledge_browser_gui_summary.v1"
KNOWLEDGE_CLASSIFICATION = "Form 0b / GUI knowledge visualization"
KNOWLEDGE_REQUIRED_AUTHORITY_KEYS = {
    "physical_execution_invoked",
    "physical_form1_claimed",
    "physical_success_claimed",
    "hardware_target_allowed",
    "dispatch_authority_created",
    "delivery_completion_claimed",
    "llm_gate_judge_used",
    "approval_free_stronger_execution",
    "public_sync_performed",
}
MAX_CARDS_PER_SECTION = 6
LIVE_SITL_RUN_ROOT = Path("output/mission_designer_live_sitl_runs")


@dataclass(frozen=True)
class ArtifactCandidate:
    path: Path
    relative_path: str
    payload: dict[str, Any]
    unreadable: bool = False
    error: str = ""


def _plain_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _field_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if value is not None and value != "" and value != []
    }


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    return (payload, "") if isinstance(payload, dict) else ({}, "json root is not object")


def _candidate_sort_key(path: Path) -> tuple[float, str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, path.as_posix())


def _latest_candidates(
    root: Path,
    filename: str,
    *,
    limit: int = MAX_CARDS_PER_SECTION,
    include_negative: bool = False,
) -> list[ArtifactCandidate]:
    if not root.exists():
        return []
    candidates = sorted(
        root.rglob(filename),
        key=_candidate_sort_key,
        reverse=True,
    )
    result: list[ArtifactCandidate] = []
    for path in candidates:
        if not include_negative and not _positive_evidence_path(path):
            continue
        payload, error = _read_json(path)
        result.append(
            ArtifactCandidate(
                path=path,
                relative_path=_relative(path),
                payload=payload,
                unreadable=bool(error),
                error=error,
            )
        )
        if len(result) >= limit:
            break
    return result


def _merge_latest_candidates(
    groups: list[list[ArtifactCandidate]],
    *,
    limit: int = MAX_CARDS_PER_SECTION,
) -> list[ArtifactCandidate]:
    candidates = [candidate for group in groups for candidate in group]
    return sorted(candidates, key=lambda candidate: _candidate_sort_key(candidate.path), reverse=True)[
        :limit
    ]


def _explicit_key_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        if key in value:
            return value.get(key)
        for nested in value.values():
            found = _explicit_key_value(nested, key)
            if found is not None:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _explicit_key_value(nested, key)
            if found is not None:
                return found
    return None


def _nested_values(value: Any, keys: set[str]) -> list[Any]:
    values: list[Any] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in keys:
                values.append(nested)
            values.extend(_nested_values(nested, keys))
    elif isinstance(value, list):
        for nested in value:
            values.extend(_nested_values(nested, keys))
    return values


def _failure_reasons(payload: Mapping[str, Any]) -> list[str]:
    keys = {
        "blocked_reasons",
        "failure_reasons",
        "blocked_reason",
        "failure_reason",
        "failure_category",
    }
    reasons: list[str] = []
    for value in _nested_values(payload, keys):
        if isinstance(value, list):
            reasons.extend(str(item) for item in value if item)
        elif value:
            reasons.append(str(value))
    return list(dict.fromkeys(reasons))


def _payload_bool(payload: Mapping[str, Any], key: str) -> Any:
    return _explicit_key_value(payload, key)


def _authority_boundary(payloads: list[Mapping[str, Any]]) -> dict[str, Any]:
    authority = _authority_false_summary(payloads)
    missing = sorted(
        key
        for key in KNOWLEDGE_REQUIRED_AUTHORITY_KEYS
        if not payloads
        or any(_explicit_key_value(payload, key) is not False for payload in payloads)
    )
    authority["authority_boundary_explicit"] = not missing
    authority["authority_missing_false_keys"] = missing
    authority["knowledge_surface_mutates_runtime"] = False
    authority["knowledge_progress_counted"] = False
    return authority


def _boundary_warnings(authority: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    if authority.get("authority_boundary_supported") is not True:
        warnings.append("authority boundary contains true forbidden flags")
    if authority.get("authority_boundary_explicit") is not True:
        warnings.append("authority boundary has missing forbidden-flag evidence")
    return warnings


def _card(
    *,
    card_id: str,
    section: str,
    title: str,
    status: str,
    artifact_path: str,
    failure_mode_id: str,
    summary: str,
    fields: Mapping[str, Any],
    observed_evidence: list[str] | None = None,
    missing_evidence: list[str] | None = None,
    recommended_next_inspection: str = "",
    boundary_status: str = "safe",
) -> dict[str, Any]:
    return {
        "card_id": card_id,
        "section": section,
        "title": title,
        "status": status,
        "artifact_path": artifact_path,
        "failure_mode_id": failure_mode_id,
        "summary": summary,
        "fields": _field_values(fields),
        "observed_evidence": observed_evidence or [],
        "missing_evidence": missing_evidence or [],
        "recommended_next_inspection": recommended_next_inspection,
        "boundary_status": boundary_status,
        "lesson_candidate": True,
        "policy_update": False,
        "progress_counted": False,
    }


def _unreadable_card(candidate: ArtifactCandidate, *, section: str) -> dict[str, Any]:
    return _card(
        card_id=f"unreadable:{candidate.relative_path}",
        section=section,
        title="Unreadable artifact",
        status="blocked",
        artifact_path=candidate.relative_path,
        failure_mode_id="artifact_unreadable",
        summary="Source artifact could not be parsed; no older success is substituted.",
        fields={"error": candidate.error},
        missing_evidence=["readable_json_object"],
        recommended_next_inspection="Inspect the artifact file and regenerate evidence if needed.",
        boundary_status="blocked",
    )


def _live_sitl_card(candidate: ArtifactCandidate) -> dict[str, Any]:
    if candidate.unreadable:
        return _unreadable_card(candidate, section="live_sitl_partials")
    payload = candidate.payload
    failure_category = str(
        payload.get("failure_category")
        or payload.get("result_status")
        or payload.get("execution_status")
        or "live_sitl_partial"
    )
    failure_reasons = _failure_reasons(payload)
    upload_observed = _payload_bool(payload, "actual_sitl_mission_upload_observed")
    if upload_observed is None:
        upload_observed = _payload_bool(payload, "mission_upload_observed")
    flight_observed = _payload_bool(payload, "actual_sitl_flight_evidence_observed")
    payload_observed = _payload_bool(payload, "payload_release_observed")
    dropoff_verified = _payload_bool(payload, "dropoff_verified")
    status = "blocked" if failure_reasons else "observed"
    if status == "observed" and not (flight_observed and dropoff_verified):
        status = "partial"
    missing = []
    if flight_observed is not True:
        missing.append("observed_flight_evidence")
    if payload_observed is not True:
        missing.append("payload_release_observation")
    if dropoff_verified is not True:
        missing.append("dropoff_verification")
    observed = []
    if upload_observed is True:
        observed.append("mission_upload")
    if flight_observed is True:
        observed.append("flight_evidence")
    if payload_observed is True:
        observed.append("payload_release")
    if dropoff_verified is True:
        observed.append("dropoff_verification")
    return _card(
        card_id=f"live_sitl:{candidate.relative_path}",
        section="live_sitl_partials",
        title="Live SITL diagnostic",
        status=status,
        artifact_path=candidate.relative_path,
        failure_mode_id=failure_category,
        summary="; ".join(failure_reasons[:2]) or "Live SITL evidence is complete enough for review.",
        fields={
            "task": payload.get("task_id") or payload.get("task"),
            "failure_category": payload.get("failure_category"),
            "result_status": payload.get("result_status") or payload.get("status"),
            "upload_observed": upload_observed,
            "flight_observed": flight_observed,
            "payload_release_observed": payload_observed,
            "dropoff_verified": dropoff_verified,
            "stdout_log": payload.get("stdout_log"),
            "stderr_log": payload.get("stderr_log"),
        },
        observed_evidence=observed,
        missing_evidence=missing,
        recommended_next_inspection=(
            "Inspect failure receipt logs first; do not infer delivery success "
            "from upload-only evidence."
        )
        if status != "observed"
        else "Inspect attached flight, payload, and dropoff artifacts.",
        boundary_status="warning" if status == "partial" else status,
    )


def _gateway_probe_card(candidate: ArtifactCandidate) -> dict[str, Any]:
    if candidate.unreadable:
        return _unreadable_card(candidate, section="blocked_probes")
    payload = candidate.payload
    reasons = _failure_reasons(payload)
    source_checks = {
        "cycle1_gateway_ref_chain_consistent": payload.get(
            "cycle1_gateway_ref_chain_consistent"
        ),
        "cycle2_gateway_ref_chain_consistent": payload.get(
            "cycle2_gateway_ref_chain_consistent"
        ),
        "gateway_loop_same_session_evidence": payload.get(
            "gateway_loop_same_session_evidence"
        ),
        "gateway_runtime_source_ref_consistent": payload.get(
            "gateway_runtime_source_ref_consistent"
        ),
        "gateway_runtime_source_path_consistent": payload.get(
            "gateway_runtime_source_path_consistent"
        ),
    }
    source_consistent = all(value is True for value in source_checks.values())
    forged = "forged" in candidate.relative_path or payload.get("forged_socket_kind") is True
    full_loop = payload.get("full_gateway_runtime_loop") is True
    status = "blocked" if reasons or not source_consistent or forged else "observed" if full_loop else "partial"
    missing = []
    missing.extend(
        key for key, value in source_checks.items() if value is not True
    )
    if not full_loop:
        missing.append("full_gateway_runtime_loop")
    if forged:
        missing.append("non_forged_gateway_boundary")
    return _card(
        card_id=f"gateway:{candidate.relative_path}",
        section="blocked_probes",
        title="Gateway / C5b probe diagnostic",
        status=status,
        artifact_path=candidate.relative_path,
        failure_mode_id=payload.get("gateway_runtime_probe_status")
        or payload.get("probe_status")
        or ("forged_gateway_probe" if forged else "gateway_probe_partial"),
        summary=(
            "Gateway probe is source-bound and observed."
            if status == "observed"
            else "Gateway probe evidence is blocked/partial; inspect source refs before reusing it."
        ),
        fields={
            "gateway_runtime_probe_status": payload.get("gateway_runtime_probe_status"),
            "full_gateway_runtime_loop": payload.get("full_gateway_runtime_loop"),
            "gateway_loop_same_session_evidence": payload.get(
                "gateway_loop_same_session_evidence"
            ),
            "cycle1_gateway_ref_chain_consistent": payload.get(
                "cycle1_gateway_ref_chain_consistent"
            ),
            "cycle2_gateway_ref_chain_consistent": payload.get(
                "cycle2_gateway_ref_chain_consistent"
            ),
            "source_runtime_run_mode": payload.get("source_runtime_run_mode"),
            "gateway_runtime_source_ref_consistent": payload.get(
                "gateway_runtime_source_ref_consistent"
            ),
            "gateway_runtime_source_path_consistent": payload.get(
                "gateway_runtime_source_path_consistent"
            ),
            "blocked_reasons": reasons,
        },
        observed_evidence=["gateway_runtime_loop"] if full_loop and source_consistent else [],
        missing_evidence=missing,
        recommended_next_inspection="Inspect gateway runtime probe refs and boundary signatures.",
        boundary_status="safe" if status == "observed" else "blocked",
    )


def _physical_seed_card(candidate: ArtifactCandidate) -> dict[str, Any]:
    if candidate.unreadable:
        return _unreadable_card(candidate, section="failure_modes")
    payload = candidate.payload
    reasons = _failure_reasons(payload)
    causal_transfer = _payload_bool(payload, "causal_verification_transferred")
    form1_required = _payload_bool(payload, "physical_form1_required")
    plan_status = str(payload.get("plan_status") or payload.get("consumption_status") or "unknown")
    blocked_parameters = payload.get("blocked_parameters") or payload.get(
        "planned_parameters_outside_envelope"
    )
    status = "blocked" if reasons or blocked_parameters else "partial"
    if plan_status == "physical_test_plan_seed_ready" and causal_transfer is False and form1_required is True:
        status = "candidate"
    if causal_transfer is True or form1_required is False:
        status = "blocked"
    missing = []
    if causal_transfer is not False:
        missing.append("causal_verification_transferred=false")
    if form1_required is not True:
        missing.append("physical_form1_required=true")
    if blocked_parameters:
        missing.append("in_range_parameters")
    return _card(
        card_id=f"physical_seed:{candidate.relative_path}",
        section="failure_modes",
        title="Physical seed boundary diagnostic",
        status=status,
        artifact_path=candidate.relative_path,
        failure_mode_id="physical_seed_boundary",
        summary=(
            "Physical seed is a candidate planning input only; it is not physical Form 1."
        ),
        fields={
            "plan_status": plan_status,
            "blocked_parameters": blocked_parameters,
            "causal_verification_transferred": causal_transfer,
            "physical_form1_required": form1_required,
            "transfer_scope": payload.get("transfer_scope"),
            "blocked_reasons": reasons,
        },
        observed_evidence=["physical_seed_ready"] if status == "candidate" else [],
        missing_evidence=missing,
        recommended_next_inspection="Inspect envelope bounds and physical seed plan before any physical backend design.",
        boundary_status="blocked" if status == "blocked" else "warning",
    )


def _read_text_tail(path: Path, *, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def _live_run_directory_cards(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    task_dirs = sorted(
        [path for path in root.iterdir() if path.is_dir()],
        key=_candidate_sort_key,
        reverse=True,
    )
    cards: list[dict[str, Any]] = []
    for task_dir in task_dirs:
        if len(cards) >= MAX_CARDS_PER_SECTION:
            break
        summaries = sorted(task_dir.rglob("summary.json"), key=_candidate_sort_key, reverse=True)
        if summaries:
            continue
        pose_logs = sorted(task_dir.rglob("pose_samples.jsonl"), key=_candidate_sort_key, reverse=True)
        stdout_logs = sorted(task_dir.glob("*_stdout.log"), key=_candidate_sort_key, reverse=True)
        stderr_logs = sorted(task_dir.glob("*_stderr.log"), key=_candidate_sort_key, reverse=True)
        if not (pose_logs or stdout_logs or stderr_logs):
            continue
        stderr_tail = _read_text_tail(stderr_logs[0]) if stderr_logs else ""
        stdout_tail = _read_text_tail(stdout_logs[0]) if stdout_logs else ""
        combined_tail = "\n".join([stderr_tail, stdout_tail])
        failure_mode = (
            "takeoff_or_climb_predicate_timeout"
            if "timed out waiting for z predicate" in combined_tail
            else "live_sitl_partial_without_summary"
        )
        cards.append(
            _card(
                card_id=f"live_run_dir:{_relative(task_dir)}",
                section="live_sitl_partials",
                title="Live SITL run directory diagnostic",
                status="blocked",
                artifact_path=_relative(task_dir),
                failure_mode_id=failure_mode,
                summary=(
                    "Live run directory has pose/log artifacts but no summary.json; "
                    "do not infer delivery success."
                ),
                fields={
                    "task": task_dir.name,
                    "pose_log": _relative(pose_logs[0]) if pose_logs else "",
                    "stdout_log": _relative(stdout_logs[0]) if stdout_logs else "",
                    "stderr_log": _relative(stderr_logs[0]) if stderr_logs else "",
                    "summary_json": "missing",
                },
                observed_evidence=["pose_samples"] if pose_logs else [],
                missing_evidence=["summary_json", "dropoff_verification"],
                recommended_next_inspection="Inspect stdout/stderr and attach structured failure receipt if needed.",
                boundary_status="warning",
            )
        )
    return cards


def _recovery_episode_card(candidate: ArtifactCandidate) -> dict[str, Any]:
    if candidate.unreadable:
        return _unreadable_card(candidate, section="recovery_episodes")
    payload = candidate.payload
    cycles = _list(payload.get("cycles") or _plain_mapping(payload.get("mission_os_supervisor_recovery_loop")).get("cycles"))
    statuses = [str(_plain_mapping(cycle).get("cycle_status") or "") for cycle in cycles]
    actions = []
    for cycle in cycles:
        cycle_map = _plain_mapping(cycle)
        request = _plain_mapping(cycle_map.get("action_request"))
        decision = _plain_mapping(cycle_map.get("decision"))
        actions.append(
            request.get("bounded_action")
            or decision.get("selected_bounded_action")
            or cycle_map.get("bounded_action")
        )
    status = "candidate" if cycles else "partial"
    if any(item and item not in {"observed", "complete"} for item in statuses):
        status = "partial"
    return _card(
        card_id=f"recovery:{candidate.relative_path}",
        section="recovery_episodes",
        title="Recovery episode candidate",
        status=status,
        artifact_path=candidate.relative_path,
        failure_mode_id=payload.get("primary_trigger") or "recovery_episode_candidate",
        summary="Recovery episode is reusable reading material only, not policy.",
        fields={
            "causal_form": payload.get("causal_form"),
            "progress_counted": payload.get("progress_counted"),
            "cycle_count": payload.get("cycle_count") or len(cycles),
            "primary_trigger": payload.get("primary_trigger"),
            "actions": [action for action in actions if action],
            "audit_status": payload.get("audit_status"),
        },
        observed_evidence=["cycle_refs"] if cycles else [],
        missing_evidence=[] if cycles else ["cycles"],
        recommended_next_inspection="Open the causal timeline for cycle-level refs before promoting any lesson.",
        boundary_status="safe" if status == "candidate" else "warning",
    )


def _next_inspection(cards: list[Mapping[str, Any]]) -> dict[str, Any]:
    priority = {"blocked": 0, "partial": 1, "candidate": 2, "observed": 3}
    ordered = sorted(cards, key=lambda card: priority.get(str(card.get("status")), 4))
    first = ordered[0] if ordered else {}
    return {
        "status": first.get("status") or "missing",
        "artifact_path": first.get("artifact_path") or "",
        "failure_mode_id": first.get("failure_mode_id") or "",
        "recommended_next_inspection": first.get("recommended_next_inspection")
        or "No knowledge artifacts found.",
    }


def build_missionos_knowledge_browser_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    live_run_root: Path | str = LIVE_SITL_RUN_ROOT,
) -> dict[str, Any]:
    """Return a read-only MissionOS knowledge browser summary."""

    root = Path(artifact_root)
    live_root = Path(live_run_root)
    if Path(live_run_root) == LIVE_SITL_RUN_ROOT and root != ARTIFACT_ROOT:
        live_root = root.parent / "mission_designer_live_sitl_runs"
    live_candidates = (
        _latest_candidates(root, "px4_gazebo_mission_designer_sitl_live_flight_failed_receipt.json")
        + _latest_candidates(root, "px4_gazebo_mission_designer_sitl_execution_result.json")
    )[:MAX_CARDS_PER_SECTION]
    gateway_candidates = _merge_latest_candidates(
        [
            _latest_candidates(
                root,
                "gateway_live_runtime_probe.json",
                include_negative=True,
            ),
            _latest_candidates(
                root,
                "gateway_live_runtime_probe_forged_socket.json",
                include_negative=True,
            ),
            _latest_candidates(
                root,
                "gateway_supervisor_process_probe_boundary.json",
                include_negative=True,
            ),
            _latest_candidates(
                root,
                "gateway_route_invocation_boundary.json",
                include_negative=True,
            ),
        ]
    )
    seed_candidates = _latest_candidates(root, "wind_form3_physical_envelope_consumption_plan.json")
    recovery_candidates = _latest_candidates(root, "mission_os_multi_condition_supervisor_runtime.json")

    cards: list[dict[str, Any]] = []
    cards.extend(_live_sitl_card(candidate) for candidate in live_candidates)
    cards.extend(_live_run_directory_cards(live_root))
    cards.extend(_gateway_probe_card(candidate) for candidate in gateway_candidates)
    cards.extend(_physical_seed_card(candidate) for candidate in seed_candidates[:3])
    cards.extend(_recovery_episode_card(candidate) for candidate in recovery_candidates[:3])

    selected_payloads = [
        candidate.payload
        for candidate in [
            *live_candidates,
            *gateway_candidates,
            *seed_candidates[:3],
            *recovery_candidates[:3],
        ]
        if candidate.payload
    ]
    authority = _authority_boundary(selected_payloads)
    warnings = _boundary_warnings(authority)
    if any(card.get("boundary_status") == "blocked" for card in cards):
        warnings.append("one or more knowledge cards are blocked")
    blocked_count = sum(1 for card in cards if card.get("status") == "blocked")
    partial_count = sum(1 for card in cards if card.get("status") == "partial")
    candidate_count = sum(1 for card in cards if card.get("status") == "candidate")
    observed_count = sum(1 for card in cards if card.get("status") == "observed")
    if blocked_count:
        browser_status = "blocked"
    elif partial_count or warnings:
        browser_status = "partial"
    elif cards:
        browser_status = "observed"
    else:
        browser_status = "missing"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_root": _relative(root),
        "browser_label": "Failure notes / next inspection",
        "browser_status": browser_status,
        "classification": {
            "causal_form": "Form 0b",
            "surface": "GUI knowledge visualization",
            "progress_counted": False,
            "runtime_capability_added": False,
            "policy_update_created": False,
        },
        "authority_boundary": authority,
        "boundary_warnings": list(dict.fromkeys(warnings)),
        "summary": {
            "failure_mode_count": sum(1 for card in cards if card.get("section") == "failure_modes"),
            "recovery_episode_candidate_count": sum(
                1 for card in cards if card.get("section") == "recovery_episodes"
            ),
            "blocked_probe_count": sum(1 for card in cards if card.get("section") == "blocked_probes"),
            "live_sitl_partial_count": sum(
                1 for card in cards if card.get("section") == "live_sitl_partials"
            ),
            "blocked_count": blocked_count,
            "partial_count": partial_count,
            "candidate_count": candidate_count,
            "observed_count": observed_count,
        },
        "next_inspection": _next_inspection(cards),
        "cards": cards,
        "not_claimed": [
            "physical_execution",
            "physical_form1",
            "physical_success",
            "hardware_target_authority",
            "dispatch_authority_creation",
            "delivery_completion",
            "public_sync",
            "policy_update",
        ],
        "operator_note": (
            "This view summarizes persisted failure / recovery artifacts as "
            "diagnostic notes for operator readability. It does not persist "
            "lessons, run SITL, probe Gateway routes, promote notes into policy, "
            "authorize dispatch, create recovery rules, or claim progress."
        ),
        "authority_false_keys": sorted(AUTHORITY_FALSE_KEYS),
        "knowledge_classification": KNOWLEDGE_CLASSIFICATION,
    }
