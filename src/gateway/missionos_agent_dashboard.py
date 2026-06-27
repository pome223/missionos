"""Read-only MissionOS higher-order agent dashboard scaffold.

The dashboard describes future MissionOS agent responsibilities and their
current evidence inputs. It must not start agents, schedule background work,
invoke SITL, probe Gateway routes, create policy updates, or mutate mission
state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from src.gateway.missionos_knowledge_browser import (
    build_missionos_knowledge_browser_summary,
)
from src.gateway.missionos_milestone import ARTIFACT_ROOT, _relative


SCHEMA_VERSION = "missionos_agent_dashboard_gui_summary.v1"
AGENT_CLASSIFICATION = "Form 0b / GUI agent-status visualization"
AGENT_BOUNDARY_FALSE_FLAGS = {
    "physical_execution_invoked": False,
    "physical_form1_claimed": False,
    "hardware_target_allowed": False,
    "dispatch_authority_created": False,
    "delivery_completion_claimed": False,
    "public_sync_performed": False,
    "policy_update_applied": False,
    "automatic_recovery_rule_created": False,
    "agent_execution_started": False,
}
AGENT_EXTRA_FORBIDDEN_FLAGS = {
    "policy_update_applied",
    "automatic_recovery_rule_created",
    "agent_execution_started",
}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _knowledge_cards_by_section(knowledge: Mapping[str, Any], section: str) -> list[Mapping[str, Any]]:
    return [
        card
        for card in _as_list(knowledge.get("cards"))
        if isinstance(card, Mapping) and card.get("section") == section
    ]


def _artifact_refs(cards: list[Mapping[str, Any]], *, limit: int = 4) -> list[str]:
    refs: list[str] = []
    for card in cards:
        ref = card.get("artifact_path")
        if ref:
            refs.append(str(ref))
    return list(dict.fromkeys(refs))[:limit]


def _authority_blocked(knowledge: Mapping[str, Any]) -> bool:
    boundary = _as_mapping(knowledge.get("authority_boundary"))
    if boundary.get("authority_boundary_supported") is False:
        return True
    for key in AGENT_BOUNDARY_FALSE_FLAGS:
        value = boundary.get(key)
        if value is True:
            return True
    return False


def _nested_truthy_paths(value: Any, keys: set[str], prefix: str = "") -> list[str]:
    if isinstance(value, Mapping):
        paths: list[str] = []
        for key, nested in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            if key in keys and nested is True:
                paths.append(next_prefix)
            paths.extend(_nested_truthy_paths(nested, keys, next_prefix))
        return paths
    if isinstance(value, list):
        paths: list[str] = []
        for index, nested in enumerate(value):
            next_prefix = f"{prefix}[{index}]"
            paths.extend(_nested_truthy_paths(nested, keys, next_prefix))
        return paths
    return []


def _extra_agent_forbidden_true_paths(root: Path) -> list[str]:
    if not root.exists():
        return []
    true_paths: list[str] = []
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        for nested_path in _nested_truthy_paths(payload, AGENT_EXTRA_FORBIDDEN_FLAGS):
            true_paths.append(f"{_relative(path)}.{nested_path}")
    return true_paths


def _boundary_flags(boundary_status: str) -> dict[str, Any]:
    flags = dict(AGENT_BOUNDARY_FALSE_FLAGS)
    flags["boundary_status"] = boundary_status
    return flags


def _agent_card(
    *,
    agent_id: str,
    label: str,
    status: str,
    role: str,
    inputs: list[str],
    outputs: list[str],
    authority_scope: str,
    disabled_reason: str,
    next_required_evidence: str,
    related_artifacts: list[str] | None = None,
    boundary_status: str = "safe",
    input_candidate_count: int = 0,
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "label": label,
        "status": status,
        "role": role,
        "inputs": inputs,
        "outputs": outputs,
        "authority_scope": authority_scope,
        "disabled_reason": disabled_reason,
        "next_required_evidence": next_required_evidence,
        "related_artifacts": related_artifacts or [],
        "boundary_status": boundary_status,
        "boundary_flags": _boundary_flags(boundary_status),
        "input_candidate_count": input_candidate_count,
        "progress_counted": False,
        "policy_update_applied": False,
        "automatic_recovery_rule_created": False,
        "agent_execution_started": False,
    }


def _block_agent_for_authority(card: dict[str, Any], reason: str) -> dict[str, Any]:
    blocked = dict(card)
    blocked["status"] = "blocked"
    blocked["boundary_status"] = "blocked"
    blocked["disabled_reason"] = reason
    blocked["boundary_flags"] = _boundary_flags("blocked")
    return blocked


def build_missionos_agent_dashboard_summary(
    *,
    artifact_root: Path | str = ARTIFACT_ROOT,
    live_run_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return a read-only higher-order MissionOS agent dashboard."""

    knowledge_kwargs: dict[str, Any] = {"artifact_root": artifact_root}
    if live_run_root is not None:
        knowledge_kwargs["live_run_root"] = live_run_root
    knowledge = build_missionos_knowledge_browser_summary(**knowledge_kwargs)
    root = Path(artifact_root)
    extra_forbidden_true_paths = _extra_agent_forbidden_true_paths(root)
    boundary_blocked = _authority_blocked(knowledge) or bool(extra_forbidden_true_paths)
    boundary_reason = "authority boundary contains true forbidden flags"

    knowledge_cards = [
        card for card in _as_list(knowledge.get("cards")) if isinstance(card, Mapping)
    ]
    blocked_probes = _knowledge_cards_by_section(knowledge, "blocked_probes")
    failure_cards = _knowledge_cards_by_section(knowledge, "failure_modes")
    recovery_cards = _knowledge_cards_by_section(knowledge, "recovery_episodes")
    live_partials = _knowledge_cards_by_section(knowledge, "live_sitl_partials")
    next_inspection = _as_mapping(knowledge.get("next_inspection"))
    source_malformed = any(
        card.get("failure_mode_id") == "artifact_unreadable" for card in knowledge_cards
    )

    gateway_candidate_count = len(blocked_probes)
    curator_candidate_count = len(knowledge_cards)
    failure_candidate_count = len(failure_cards) + len(live_partials)
    recovery_candidate_count = len(recovery_cards)

    cards = [
        _agent_card(
            agent_id="gateway_agent",
            label="Gateway Agent",
            status=(
                "candidate_inputs_available"
                if gateway_candidate_count or recovery_candidate_count
                else "disabled_missing_evidence"
            ),
            role="Owns future Gateway runtime orchestration status, not a start control.",
            inputs=[
                "gateway runtime probe evidence",
                "blocked Gateway probe cards",
                "causal timeline source refs",
            ],
            outputs=[
                "agent readiness summary",
                "next Gateway evidence requirement",
            ],
            authority_scope="read-only status projection; no Gateway probe invocation",
            disabled_reason=(
                ""
                if gateway_candidate_count or recovery_candidate_count
                else "Gateway-owned runtime evidence is missing."
            ),
            next_required_evidence=(
                "Inspect blocked Gateway probe cards and source-bound refs before any runtime work."
                if gateway_candidate_count
                else "Materialize source-bound Gateway runtime evidence."
            ),
            related_artifacts=_artifact_refs([*blocked_probes, *recovery_cards]),
            input_candidate_count=gateway_candidate_count + recovery_candidate_count,
        ),
        _agent_card(
            agent_id="knowledge_curator",
            label="Knowledge Curator",
            status=(
                "candidate_inputs_available"
                if curator_candidate_count
                else "disabled_missing_evidence"
            ),
            role="Reads failure / recovery knowledge candidates for human review.",
            inputs=[
                "failure mode cards",
                "recovery episode candidates",
                "next inspection hints",
            ],
            outputs=[
                "curation candidate list",
                "operator-readable knowledge summary",
            ],
            authority_scope="diagnostic knowledge only; no policy update or recovery rule",
            disabled_reason=(
                ""
                if curator_candidate_count
                else "No failure / recovery knowledge cards are available."
            ),
            next_required_evidence=(
                next_inspection.get("recommended_next_inspection")
                or "Review the newest blocked / partial knowledge card."
            ),
            related_artifacts=_artifact_refs(knowledge_cards),
            input_candidate_count=curator_candidate_count,
        ),
        _agent_card(
            agent_id="experiment_planner",
            label="Experiment Planner",
            status="disabled_missing_evidence" if failure_candidate_count else "future_only",
            role="Future planner for operator-reviewed experiment proposals.",
            inputs=[
                "failure mode candidates",
                "envelope gaps",
                "operator-approved experiment scope",
            ],
            outputs=[
                "experiment proposal candidate",
                "manual verification checklist",
            ],
            authority_scope="future-only proposal surface; no automatic planning or SITL start",
            disabled_reason=(
                "Failure candidates exist, but automatic experiment planning is not implemented."
                if failure_candidate_count
                else "No experiment-planning backend exists."
            ),
            next_required_evidence="Define a human-reviewed experiment proposal schema before enabling this agent.",
            related_artifacts=_artifact_refs([*failure_cards, *live_partials]),
            input_candidate_count=failure_candidate_count,
        ),
        _agent_card(
            agent_id="physical_readiness_agent",
            label="Physical Readiness Agent",
            status="disabled_missing_approval_package",
            role="Future physical-readiness reviewer for approval package evidence.",
            inputs=[
                "physical seed boundary diagnostics",
                "approval package refs",
                "physical safety operator refs",
            ],
            outputs=[
                "physical readiness candidate",
            ],
            authority_scope="readiness visualization only; no physical execution or Form 1 claim",
            disabled_reason="No physical approval package, physical Form 1, or hardware authority exists.",
            next_required_evidence=(
                "Materialize the full approval package before any physical-readiness review."
            ),
            related_artifacts=_artifact_refs(
                [
                    card
                    for card in knowledge_cards
                    if "physical" in str(card.get("failure_mode_id", "")).lower()
                    or "seed" in str(card.get("title", "")).lower()
                ]
            ),
            input_candidate_count=0,
        ),
        _agent_card(
            agent_id="fleet_agent",
            label="Fleet Agent",
            status="future_only",
            role="Future multi-vehicle / fleet coordination status surface.",
            inputs=[
                "multi-drone backend evidence",
                "fleet session refs",
                "vehicle identity contracts",
            ],
            outputs=[
                "fleet readiness candidate",
            ],
            authority_scope="future-only visualization; no multi-drone backend or dispatch",
            disabled_reason="No fleet or multi-drone backend exists.",
            next_required_evidence="Create source-bound multi-vehicle evidence before enabling this surface.",
            related_artifacts=[],
            input_candidate_count=0,
        ),
    ]

    if boundary_blocked:
        cards = [_block_agent_for_authority(card, boundary_reason) for card in cards]
    if source_malformed:
        cards = [
            _block_agent_for_authority(card, "source artifact is malformed or unreadable")
            for card in cards
        ]

    status_counts: dict[str, int] = {}
    for card in cards:
        status = str(card.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    blocked_count = status_counts.get("blocked", 0)
    if blocked_count:
        dashboard_status = "blocked"
    elif status_counts.get("candidate_inputs_available"):
        dashboard_status = "candidate_inputs_available"
    else:
        dashboard_status = "future_only"

    boundary_warnings = list(_as_list(knowledge.get("boundary_warnings")))
    if boundary_blocked:
        boundary_warnings.append(boundary_reason)
    if extra_forbidden_true_paths:
        boundary_warnings.append("agent forbidden flags are true in source artifacts")
    if source_malformed:
        boundary_warnings.append("source artifact is malformed or unreadable")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_root": _relative(Path(artifact_root)),
        "dashboard_label": "Future agent roles - not running",
        "dashboard_status": dashboard_status,
        "classification": {
            "causal_form": "Form 0b",
            "surface": "GUI agent-status visualization",
            "progress_counted": False,
            "runtime_capability_added": False,
            "agent_execution_started": False,
            "policy_update_created": False,
        },
        "authority_boundary": {
            **AGENT_BOUNDARY_FALSE_FLAGS,
            "authority_boundary_supported": not boundary_blocked,
            "knowledge_authority_boundary_explicit": _as_mapping(
                knowledge.get("authority_boundary")
            ).get("authority_boundary_explicit")
            is True,
            "agent_forbidden_true_paths": extra_forbidden_true_paths,
        },
        "boundary_warnings": list(dict.fromkeys(str(item) for item in boundary_warnings if item)),
        "summary": {
            "agent_count": len(cards),
            "candidate_input_agent_count": status_counts.get("candidate_inputs_available", 0),
            "future_only_agent_count": status_counts.get("future_only", 0),
            "disabled_agent_count": sum(
                count for status, count in status_counts.items() if status.startswith("disabled_")
            ),
            "blocked_agent_count": blocked_count,
            "knowledge_input_candidate_count": curator_candidate_count,
            "gateway_input_candidate_count": gateway_candidate_count,
        },
        "agents": cards,
        "knowledge_input": {
            "schema_version": knowledge.get("schema_version"),
            "browser_status": knowledge.get("browser_status"),
            "next_inspection": next_inspection,
            "candidate_cards_available": curator_candidate_count,
            "policy_update_applied": False,
            "automatic_recovery_rule_created": False,
        },
        "not_claimed": [
            "agent_execution",
            "background_automation",
            "live_sitl",
            "gateway_probe",
            "dispatch_authority_creation",
            "physical_execution",
            "physical_form1",
            "hardware_authority",
            "delivery_completion",
            "public_sync",
            "policy_update",
            "automatic_recovery_rule",
        ],
        "operator_note": (
            "This dashboard is a read-only role map for future higher-order "
            "MissionOS agents. It does not start agents, schedule background "
            "work, create policy updates, generate recovery rules, invoke SITL, "
            "probe Gateway routes, dispatch actions, or claim progress."
        ),
        **AGENT_BOUNDARY_FALSE_FLAGS,
        "agent_classification": AGENT_CLASSIFICATION,
    }
