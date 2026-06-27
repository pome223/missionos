"""Typed MissionContract presets for common Mission OS slices."""

from __future__ import annotations

from dataclasses import dataclass, field
from string import Formatter
from typing import Any

from src.runtime.mission_contract import MissionContract, build_mission_contract

_MISSION_TEMPLATE_SCHEMA_VERSION = "mission_template.v1"
_LIST_FIELDS = {
    "success_metrics": "default_success_metrics",
    "allowed_actions": "default_allowed_actions",
    "forbidden_actions": "default_forbidden_actions",
    "completion_criteria": "default_completion_criteria",
    "evidence_requirements": "default_evidence_requirements",
}
_POLICY_FIELDS = {
    "risk_budget",
    "capability_policy",
    "memory_policy",
    "recovery_policy",
    "improvement_policy",
}
_ALLOWED_OVERRIDE_FIELDS = {
    "contract_id",
    "objective",
    "success_metrics",
    "allowed_actions",
    "forbidden_actions",
    "abort_conditions",
    "completion_criteria",
    "evidence_requirements",
    "task_nodes",
    "risk_budget",
    "capability_policy",
    "memory_policy",
    "recovery_policy",
    "improvement_policy",
    "metadata",
}
_RISK_BUDGET_NUMERIC_FIELDS = {
    "max_runtime_hours",
    "max_total_llm_calls",
    "max_total_tool_calls",
    "max_same_failure_retries",
    "max_repair_depth",
    "max_pending_approvals",
}


@dataclass(frozen=True)
class MissionTemplate:
    """Reusable preset that renders into a MissionContract payload."""

    id: str
    title: str
    description: str
    objective_template: str
    required_inputs: tuple[str, ...]
    default_success_metrics: tuple[str, ...]
    default_allowed_actions: tuple[str, ...]
    default_forbidden_actions: tuple[str, ...]
    default_completion_criteria: tuple[str, ...]
    default_evidence_requirements: tuple[str, ...]
    default_recovery_policy: dict[str, Any]
    default_memory_policy: dict[str, Any]
    default_improvement_policy: dict[str, Any]
    risk_notes: tuple[str, ...]
    non_goals: tuple[str, ...]
    default_risk_budget: dict[str, Any] | None = None
    default_capability_policy: dict[str, Any] | None = None
    default_task_nodes: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def model_dump(self) -> dict[str, Any]:
        return {
            "schema_version": _MISSION_TEMPLATE_SCHEMA_VERSION,
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "objective_template": self.objective_template,
            "required_inputs": list(self.required_inputs),
            "default_success_metrics": list(self.default_success_metrics),
            "default_allowed_actions": list(self.default_allowed_actions),
            "default_forbidden_actions": list(self.default_forbidden_actions),
            "default_completion_criteria": list(self.default_completion_criteria),
            "default_evidence_requirements": list(self.default_evidence_requirements),
            "default_recovery_policy": dict(self.default_recovery_policy),
            "default_memory_policy": dict(self.default_memory_policy),
            "default_improvement_policy": dict(self.default_improvement_policy),
            "default_risk_budget": (
                dict(self.default_risk_budget)
                if self.default_risk_budget is not None
                else None
            ),
            "default_capability_policy": (
                dict(self.default_capability_policy)
                if self.default_capability_policy is not None
                else None
            ),
            "default_task_nodes": [dict(node) for node in self.default_task_nodes],
            "risk_notes": list(self.risk_notes),
            "non_goals": list(self.non_goals),
        }


class MissionTemplateError(ValueError):
    """Raised when a mission template cannot produce a safe contract."""


class _StrictInputMap(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise MissionTemplateError(f"mission template input is required: {key}")


def _clean_inputs(inputs: dict[str, Any] | None) -> dict[str, str]:
    return {str(key): str(value).strip() for key, value in (inputs or {}).items()}


def _format_objective(template: MissionTemplate, inputs: dict[str, str]) -> str:
    expected = {
        field_name
        for _, field_name, _, _ in Formatter().parse(template.objective_template)
        if field_name
    }
    missing = sorted(
        field_name
        for field_name in set(template.required_inputs) | expected
        if not inputs.get(field_name)
    )
    if missing:
        raise MissionTemplateError(
            "mission template missing required inputs: " + ", ".join(missing)
        )
    return template.objective_template.format_map(_StrictInputMap(inputs))


def _ordered_union(left: list[str], right: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in [*left, *right]:
        text = str(item or "").strip()
        if text and text not in seen:
            values.append(text)
            seen.add(text)
    return values


def _ensure_allowed_actions_narrow(
    template: MissionTemplate,
    override_actions: Any,
) -> list[str]:
    requested = [
        str(item).strip() for item in override_actions or [] if str(item).strip()
    ]
    default_actions = set(template.default_allowed_actions)
    additions = sorted(action for action in requested if action not in default_actions)
    if additions:
        raise MissionTemplateError(
            "mission template allowed_actions overrides may only narrow defaults; "
            f"unsupported additions: {', '.join(additions)}"
        )
    return requested


def _merge_numeric_budget(
    template: MissionTemplate,
    override_budget: dict[str, Any],
) -> dict[str, Any]:
    base = dict(template.default_risk_budget or {})
    merged = dict(base)
    for key, value in override_budget.items():
        if key in _RISK_BUDGET_NUMERIC_FIELDS:
            merged[key] = _reject_numeric_increase(
                field_name=f"risk_budget.{key}",
                base_value=base.get(key),
                override_value=value,
            )
            continue
        merged[key] = value
    return merged


def _coerce_int_for_safety(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise MissionTemplateError(f"mission template {field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if normalized and normalized.lstrip("+-").isdigit():
            return int(normalized)
    raise MissionTemplateError(f"mission template {field_name} must be an integer")


def _coerce_bool_for_safety(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise MissionTemplateError(f"mission template {field_name} must be a boolean")


def _clean_override_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    else:
        values = list(value or [])
    return [str(item).strip() for item in values if str(item).strip()]


def _reject_list_additions(
    *,
    field_name: str,
    base_values: Any,
    override_values: Any,
) -> list[str]:
    requested = _clean_override_list(override_values)
    allowed = set(_clean_override_list(base_values))
    additions = sorted(item for item in requested if item not in allowed)
    if additions:
        raise MissionTemplateError(
            f"mission template {field_name} override may only narrow defaults; "
            f"unsupported additions: {', '.join(additions)}"
        )
    return requested


def _reject_numeric_increase(
    *,
    field_name: str,
    base_value: Any,
    override_value: Any,
) -> int:
    override_number = _coerce_int_for_safety(override_value, field_name=field_name)
    if base_value is None:
        return override_number
    base_number = _coerce_int_for_safety(base_value, field_name=field_name)
    if override_number > base_number:
        raise MissionTemplateError(
            f"mission template {field_name} override cannot increase default"
        )
    return override_number


def _merge_recovery_policy(
    current_policy: dict[str, Any],
    override_policy: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(current_policy)
    if "max_retries_per_step" in override_policy:
        merged["max_retries_per_step"] = _reject_numeric_increase(
            field_name="recovery_policy.max_retries_per_step",
            base_value=current_policy.get("max_retries_per_step"),
            override_value=override_policy["max_retries_per_step"],
        )
    if "ladder" in override_policy:
        merged["ladder"] = _reject_list_additions(
            field_name="recovery_policy.ladder",
            base_values=current_policy.get("ladder"),
            override_values=override_policy["ladder"],
        )
    for key, value in override_policy.items():
        if key in {"ladder", "max_retries_per_step"}:
            continue
        merged[key] = value
    return merged


def _merge_memory_policy(
    current_policy: dict[str, Any],
    override_policy: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(current_policy)
    if "require_operator_approval" in override_policy:
        override_approval = _coerce_bool_for_safety(
            override_policy["require_operator_approval"],
            field_name="memory_policy.require_operator_approval",
        )
        if (
            current_policy.get("require_operator_approval") is True
            and not override_approval
        ):
            raise MissionTemplateError(
                "mission template memory_policy.require_operator_approval override "
                "cannot disable operator approval"
            )
        merged["require_operator_approval"] = override_approval
    if "candidate_ttl_seconds" in override_policy:
        merged["candidate_ttl_seconds"] = _reject_numeric_increase(
            field_name="memory_policy.candidate_ttl_seconds",
            base_value=current_policy.get("candidate_ttl_seconds"),
            override_value=override_policy["candidate_ttl_seconds"],
        )
    if "promote_only" in override_policy:
        merged["promote_only"] = _reject_list_additions(
            field_name="memory_policy.promote_only",
            base_values=current_policy.get("promote_only"),
            override_values=override_policy["promote_only"],
        )
    if "never_promote" in override_policy:
        merged["never_promote"] = _ordered_union(
            _clean_override_list(current_policy.get("never_promote")),
            _clean_override_list(override_policy["never_promote"]),
        )
    for key, value in override_policy.items():
        if key in {
            "candidate_ttl_seconds",
            "never_promote",
            "promote_only",
            "require_operator_approval",
        }:
            continue
        merged[key] = value
    return merged


def _merge_improvement_policy(
    current_policy: dict[str, Any],
    override_policy: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(current_policy)
    for key in ("require_benchmark_pass", "require_human_promotion"):
        if key not in override_policy:
            continue
        override_gate = _coerce_bool_for_safety(
            override_policy[key],
            field_name=f"improvement_policy.{key}",
        )
        if current_policy.get(key) is True and not override_gate:
            raise MissionTemplateError(
                f"mission template improvement_policy.{key} override cannot "
                "disable required gates"
            )
        merged[key] = override_gate
    if "candidate_kinds" in override_policy:
        merged["candidate_kinds"] = _reject_list_additions(
            field_name="improvement_policy.candidate_kinds",
            base_values=current_policy.get("candidate_kinds"),
            override_values=override_policy["candidate_kinds"],
        )
    for key, value in override_policy.items():
        if key in {
            "candidate_kinds",
            "require_benchmark_pass",
            "require_human_promotion",
        }:
            continue
        merged[key] = value
    return merged


def _template_payload(
    template: MissionTemplate, inputs: dict[str, str]
) -> dict[str, Any]:
    metadata = {
        "schema_version": _MISSION_TEMPLATE_SCHEMA_VERSION,
        "template_id": template.id,
        "template_title": template.title,
        "template_inputs": dict(inputs),
        "risk_notes": list(template.risk_notes),
        "non_goals": list(template.non_goals),
    }
    payload: dict[str, Any] = {
        "contract_id": f"mission-template:{template.id}",
        "objective": _format_objective(template, inputs),
        "success_metrics": list(template.default_success_metrics),
        "allowed_actions": list(template.default_allowed_actions),
        "forbidden_actions": list(template.default_forbidden_actions),
        "completion_criteria": list(template.default_completion_criteria),
        "evidence_requirements": list(template.default_evidence_requirements),
        "task_nodes": [dict(node) for node in template.default_task_nodes],
        "recovery_policy": dict(template.default_recovery_policy),
        "memory_policy": dict(template.default_memory_policy),
        "improvement_policy": dict(template.default_improvement_policy),
        "metadata": metadata,
    }
    if template.default_risk_budget is not None:
        payload["risk_budget"] = dict(template.default_risk_budget)
    if template.default_capability_policy is not None:
        payload["capability_policy"] = dict(template.default_capability_policy)
    return payload


def _apply_overrides(
    template: MissionTemplate,
    payload: dict[str, Any],
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    if not overrides:
        return payload
    unknown = sorted(set(overrides) - _ALLOWED_OVERRIDE_FIELDS)
    if unknown:
        raise MissionTemplateError(
            "mission template override contains unsupported fields: "
            + ", ".join(unknown)
        )

    merged = dict(payload)
    for field_name, template_field_name in _LIST_FIELDS.items():
        if field_name not in overrides:
            continue
        override_values = [
            str(item).strip()
            for item in overrides[field_name] or []
            if str(item).strip()
        ]
        if field_name == "allowed_actions":
            merged[field_name] = _ensure_allowed_actions_narrow(
                template,
                override_values,
            )
        elif field_name == "forbidden_actions":
            merged[field_name] = _ordered_union(
                list(getattr(template, template_field_name)),
                override_values,
            )
        else:
            merged[field_name] = override_values

    for field_name in ("contract_id", "objective", "abort_conditions", "task_nodes"):
        if field_name in overrides:
            merged[field_name] = overrides[field_name]

    for field_name in _POLICY_FIELDS:
        if field_name not in overrides:
            continue
        override_policy = overrides[field_name]
        if field_name == "risk_budget" and isinstance(override_policy, dict):
            merged[field_name] = _merge_numeric_budget(template, override_policy)
            continue
        if field_name == "recovery_policy" and isinstance(override_policy, dict):
            merged[field_name] = _merge_recovery_policy(
                dict(merged.get(field_name) or {}),
                override_policy,
            )
            continue
        if field_name == "memory_policy" and isinstance(override_policy, dict):
            merged[field_name] = _merge_memory_policy(
                dict(merged.get(field_name) or {}),
                override_policy,
            )
            continue
        if field_name == "improvement_policy" and isinstance(override_policy, dict):
            merged[field_name] = _merge_improvement_policy(
                dict(merged.get(field_name) or {}),
                override_policy,
            )
            continue
        if isinstance(override_policy, dict):
            base = dict(merged.get(field_name) or {})
            base.update(override_policy)
            merged[field_name] = base
        else:
            merged[field_name] = override_policy

    metadata = dict(merged.get("metadata") or {})
    metadata.update(dict(overrides.get("metadata") or {}))
    metadata["template_id"] = template.id
    metadata.setdefault("schema_version", _MISSION_TEMPLATE_SCHEMA_VERSION)
    metadata.setdefault("risk_notes", list(template.risk_notes))
    metadata.setdefault("non_goals", list(template.non_goals))
    merged["metadata"] = metadata
    return merged


def _base_memory_policy(*, ttl_seconds: int = 2_592_000) -> dict[str, Any]:
    return {
        "promote_only": ["failure_pattern", "recovery_pattern", "mission_summary"],
        "never_promote": ["raw_transcript", "secret", "one_off_noise"],
        "require_operator_approval": True,
        "candidate_ttl_seconds": ttl_seconds,
    }


def _base_improvement_policy() -> dict[str, Any]:
    return {
        "mode": "canary_only",
        "require_benchmark_pass": True,
        "require_human_promotion": True,
        "candidate_kinds": [
            "benchmark_case",
            "verifier_improvement",
            "recovery_strategy",
        ],
    }


_TEMPLATES: dict[str, MissionTemplate] = {
    "observation_review": MissionTemplate(
        id="observation_review",
        title="Observation review",
        description="Observe a target state and produce evidence without mutation.",
        objective_template=(
            "Observe {target} and produce an evidence-backed review without "
            "modifying state."
        ),
        required_inputs=("target",),
        default_success_metrics=(
            "target state is observed",
            "evidence refs are recorded",
            "uncertainty is called out explicitly",
        ),
        default_allowed_actions=("current_tab.info", "browser.read"),
        default_forbidden_actions=(
            "navigate away",
            "type into the page",
            "submit forms",
            "delete data",
            "change permissions",
            "install software",
        ),
        default_completion_criteria=("visible state reviewed", "evidence recorded"),
        default_evidence_requirements=("current_tab_info result", "verifier verdict"),
        default_recovery_policy={
            "max_retries_per_step": 1,
            "ladder": [
                "observe_again",
                "verify_state",
                "request_approval",
                "pause_or_block",
            ],
        },
        default_memory_policy=_base_memory_policy(),
        default_improvement_policy=_base_improvement_policy(),
        risk_notes=("read-only preset", "operator approval required for uncertainty"),
        non_goals=("no page mutation", "no account or permission changes"),
    ),
    "weak_evidence_probe": MissionTemplate(
        id="weak_evidence_probe",
        title="Weak evidence probe",
        description="Exercise weak-evidence recovery and approval escalation.",
        objective_template=(
            "Inspect {target} and treat weak or non-destination-bound evidence as "
            "operator-review-required."
        ),
        required_inputs=("target",),
        default_success_metrics=(
            "weak evidence remains uncertain",
            "approval-oriented recovery is recorded",
        ),
        default_allowed_actions=("current_tab.info",),
        default_forbidden_actions=(
            "navigate away",
            "type into the page",
            "take screenshots",
            "submit forms",
            "delete data",
        ),
        default_completion_criteria=(
            "current state inspected",
            "weak evidence classified",
        ),
        default_evidence_requirements=("current_tab_info result", "recovery decision"),
        default_recovery_policy={
            "max_retries_per_step": 0,
            "ladder": ["verify_state", "request_approval", "pause_or_block"],
        },
        default_memory_policy=_base_memory_policy(ttl_seconds=604_800),
        default_improvement_policy=_base_improvement_policy(),
        default_risk_budget={
            "max_same_failure_retries": 0,
            "max_repair_depth": 0,
            "max_pending_approvals": 1,
        },
        risk_notes=("designed to stop for operator review on weak evidence",),
        non_goals=("no automatic retry loop", "no evidence fabrication"),
    ),
    "budget_exhaustion_probe": MissionTemplate(
        id="budget_exhaustion_probe",
        title="Budget exhaustion probe",
        description="Use strict budgets to verify blocked-state behavior.",
        objective_template=(
            "Run a strict-budget probe against {target} and block when retry or "
            "approval budget is exhausted."
        ),
        required_inputs=("target",),
        default_success_metrics=(
            "budget exhaustion is visible",
            "blocked state is distinct from failed",
        ),
        default_allowed_actions=("current_tab.info",),
        default_forbidden_actions=(
            "navigate away",
            "type into the page",
            "submit forms",
            "delete data",
        ),
        default_completion_criteria=(
            "budget state inspected",
            "blocked reason recorded",
        ),
        default_evidence_requirements=("budget_state", "recovery decision"),
        default_recovery_policy={
            "max_retries_per_step": 0,
            "ladder": ["retry_same_step", "pause_or_block"],
        },
        default_memory_policy=_base_memory_policy(ttl_seconds=604_800),
        default_improvement_policy=_base_improvement_policy(),
        default_risk_budget={
            "max_same_failure_retries": 0,
            "max_repair_depth": 0,
            "max_pending_approvals": 0,
        },
        risk_notes=(
            "strict zero-retry preset",
            "expected terminal state may be blocked",
        ),
        non_goals=("no repair loop", "no automatic approval escalation"),
    ),
    "current_tab_research_to_report": MissionTemplate(
        id="current_tab_research_to_report",
        title="Current-tab research to report",
        description="Research a topic and write a source-linked local report.",
        objective_template=(
            "Research {topic} and write a source-linked report to {report_target} "
            "without submitting external forms."
        ),
        required_inputs=("topic", "report_target"),
        default_success_metrics=(
            "report contains source URLs",
            "claims are marked with uncertainty when needed",
            "report target receives the summary",
        ),
        default_allowed_actions=("web.search", "browser.read", "file.write"),
        default_forbidden_actions=(
            "submit external forms",
            "create accounts",
            "change sharing permissions",
            "delete files",
            "purchase goods or services",
        ),
        default_completion_criteria=("report written", "source links included"),
        default_evidence_requirements=(
            "source list",
            "report artifact",
            "verifier verdict",
        ),
        default_recovery_policy={
            "max_retries_per_step": 1,
            "ladder": [
                "observe_again",
                "verify_state",
                "retry_smaller_step",
                "request_approval",
                "pause_or_block",
            ],
        },
        default_memory_policy=_base_memory_policy(),
        default_improvement_policy=_base_improvement_policy(),
        default_risk_budget={"max_same_failure_retries": 1, "max_repair_depth": 1},
        default_task_nodes=(
            {"node_id": "research", "description": "Collect source-backed findings"},
            {
                "node_id": "write_report",
                "description": "Write the local report",
                "depends_on": ["research"],
            },
            {
                "node_id": "verify_report",
                "description": "Verify report evidence",
                "depends_on": ["write_report"],
            },
        ),
        risk_notes=("writes only to the requested report target",),
        non_goals=("no external posting", "no account creation", "no paid actions"),
    ),
    "repo_maintenance_review": MissionTemplate(
        id="repo_maintenance_review",
        title="Repository maintenance review",
        description="Review a repository maintenance surface without making changes.",
        objective_template=(
            "Review {repo_path} for {focus} and produce a maintenance report "
            "without modifying files."
        ),
        required_inputs=("repo_path", "focus"),
        default_success_metrics=(
            "findings are linked to files or commands",
            "test gaps are listed",
            "no files are modified",
        ),
        default_allowed_actions=(
            "repo.inspect",
            "git.status",
            "git.diff",
            "pytest.targeted",
        ),
        default_forbidden_actions=(
            "modify files",
            "delete files",
            "git reset --hard",
            "git push",
            "change remote configuration",
        ),
        default_completion_criteria=(
            "maintenance report produced",
            "verification commands listed",
        ),
        default_evidence_requirements=(
            "git status",
            "diff summary",
            "targeted test output",
        ),
        default_recovery_policy={
            "max_retries_per_step": 1,
            "ladder": [
                "observe_again",
                "verify_state",
                "diagnostic_task",
                "pause_or_block",
            ],
        },
        default_memory_policy=_base_memory_policy(),
        default_improvement_policy=_base_improvement_policy(),
        default_risk_budget={"max_same_failure_retries": 1, "max_repair_depth": 0},
        default_task_nodes=(
            {"node_id": "inspect_repo", "description": "Inspect repository state"},
            {
                "node_id": "review_findings",
                "description": "Review maintenance findings",
                "depends_on": ["inspect_repo"],
            },
            {
                "node_id": "summarize",
                "description": "Summarize maintenance report",
                "depends_on": ["review_findings"],
            },
        ),
        risk_notes=("read-only repository preset",),
        non_goals=("no commits", "no pushes", "no file edits"),
    ),
}


def list_mission_templates() -> list[dict[str, Any]]:
    """Return all mission templates as stable metadata dictionaries."""

    return [template.model_dump() for template in _TEMPLATES.values()]


def get_mission_template(template_id: str) -> MissionTemplate:
    """Return a mission template by id or raise a clear error."""

    normalized = str(template_id or "").strip()
    try:
        return _TEMPLATES[normalized]
    except KeyError as exc:
        raise MissionTemplateError(f"unknown mission template: {normalized}") from exc


def build_mission_contract_from_template(
    template_id: str,
    inputs: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> MissionContract:
    """Render a template into a validated MissionContract v2."""

    template = get_mission_template(template_id)
    normalized_inputs = _clean_inputs(inputs)
    payload = _template_payload(template, normalized_inputs)
    payload = _apply_overrides(template, payload, overrides)
    return build_mission_contract(**payload)
