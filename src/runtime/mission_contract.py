"""Runtime-neutral mission contract schema for live long-running work."""

from __future__ import annotations

from enum import Enum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MissionAbortConditionType(str, Enum):
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"
    GUARDRAIL_BUDGET_EXHAUSTED = "guardrail_budget_exhausted"
    CURRENT_TAB_CONNECTION_UNAVAILABLE = "current_tab_connection_unavailable"
    MISSION_CONTRACT_VIOLATION = "mission_contract_violation"
    TELEMETRY_HEALTH_UNSAFE = "telemetry_health_unsafe"


_DEFAULT_ABORT_CONDITION_TYPES = [
    MissionAbortConditionType.MISSION_CONTRACT_VIOLATION,
]
_DEFAULT_COMPLETION_CRITERIA = [
    "objective_satisfied",
    "evidence_recorded_for_each_iteration",
]
_DEFAULT_EVIDENCE_REQUIREMENTS = [
    "child_control_loop_result",
    "verifier_verdict",
    "checkpoint",
]


def _clean_text_list(items: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if items is None:
        return []
    if isinstance(items, str):
        candidate = items.strip()
        return [candidate] if candidate else []
    return [str(item).strip() for item in items if str(item).strip()]


def _normalize_abort_condition_type(value: Any) -> MissionAbortConditionType:
    if isinstance(value, MissionAbortConditionType):
        return value
    text = str(value or "").strip()
    normalized = text.lower().replace("-", "_").replace(" ", "_")
    return MissionAbortConditionType(normalized)


class MissionAbortCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: MissionAbortConditionType
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"type": value}
        return value

    @field_validator("type", mode="before")
    @classmethod
    def _strip_type(cls, value: Any) -> MissionAbortConditionType:
        return _normalize_abort_condition_type(value)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, value: Any) -> str:
        return str(value or "").strip()


class MissionTaskNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("node_id", "title", "description", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("depends_on", "completion_criteria", mode="before")
    @classmethod
    def _strip_text_list(cls, value: Any) -> list[str]:
        return _clean_text_list(value)


class MissionRiskBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_runtime_hours: int | None = Field(default=None, ge=1)
    max_total_llm_calls: int | None = Field(default=None, ge=1)
    max_total_tool_calls: int | None = Field(default=None, ge=1)
    max_same_failure_retries: int | None = Field(default=None, ge=0)
    max_repair_depth: int | None = Field(default=None, ge=0)
    max_pending_approvals: int | None = Field(default=None, ge=0)


class MissionCapabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    approval_required: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    @field_validator("allow", "approval_required", "deny", mode="before")
    @classmethod
    def _strip_text_list(cls, value: Any) -> list[str]:
        return _clean_text_list(value)


class MissionMemoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promote_only: list[str] = Field(
        default_factory=lambda: [
            "fact",
            "procedure",
            "failure_pattern",
            "recovery_pattern",
            "approved_improvement",
            "mission_summary",
        ]
    )
    never_promote: list[str] = Field(
        default_factory=lambda: ["raw_transcript", "secret", "one_off_noise"]
    )
    require_operator_approval: bool = True
    candidate_ttl_seconds: int | None = Field(default=2_592_000, ge=1)

    @field_validator("promote_only", "never_promote", mode="before")
    @classmethod
    def _strip_text_list(cls, value: Any) -> list[str]:
        return _clean_text_list(value)


class MissionRecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_retries_per_step: int = Field(default=2, ge=0)
    ladder: list[str] = Field(
        default_factory=lambda: [
            "observe_again",
            "verify_state",
            "retry_same_step",
            "retry_smaller_step",
            "alternate_capability",
            "diagnostic_task",
            "request_approval",
            "pause_or_block",
            "create_improvement_candidate",
        ]
    )
    terminal_statuses: list[str] = Field(
        default_factory=lambda: [
            "failed",
            "blocked",
            "paused",
            "improvement_candidate",
        ]
    )

    @field_validator("ladder", "terminal_statuses", mode="before")
    @classmethod
    def _strip_text_list(cls, value: Any) -> list[str]:
        return _clean_text_list(value)


class MissionImprovementPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "canary_only"
    require_benchmark_pass: bool = True
    require_human_promotion: bool = True
    candidate_kinds: list[str] = Field(
        default_factory=lambda: [
            "benchmark_case",
            "failure_classifier",
            "verifier_improvement",
            "recovery_strategy",
            "prompt_improvement",
            "policy_adjustment",
            "code_patch",
        ]
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _strip_mode(cls, value: Any) -> str:
        return str(value or "").strip() or "canary_only"

    @field_validator("candidate_kinds", mode="before")
    @classmethod
    def _strip_text_list(cls, value: Any) -> list[str]:
        return _clean_text_list(value)


def _clean_abort_conditions(
    items: list[Any] | tuple[Any, ...] | str | dict[str, Any] | MissionAbortCondition | None,
) -> list[MissionAbortCondition]:
    if items is None:
        return []
    if isinstance(items, MissionAbortCondition):
        return [items]
    if isinstance(items, (str, dict)):
        candidate: Any = items.strip() if isinstance(items, str) else items
        return [MissionAbortCondition.model_validate(candidate)] if candidate else []

    conditions: list[MissionAbortCondition] = []
    for item in items:
        if isinstance(item, str) and not item.strip():
            continue
        conditions.append(MissionAbortCondition.model_validate(item))
    return conditions


def _clean_task_nodes(
    items: list[Any] | tuple[Any, ...] | dict[str, Any] | MissionTaskNode | None,
) -> list[MissionTaskNode]:
    if items is None:
        return []
    if isinstance(items, MissionTaskNode):
        return [items]
    if isinstance(items, dict):
        return [MissionTaskNode.model_validate(items)]
    nodes: list[MissionTaskNode] = []
    for item in items:
        nodes.append(MissionTaskNode.model_validate(item))
    return nodes


def _default_abort_conditions() -> list[MissionAbortCondition]:
    return [
        MissionAbortCondition(type=condition_type)
        for condition_type in _DEFAULT_ABORT_CONDITION_TYPES
    ]


def _default_contract_id(objective: str) -> str:
    digest = sha256(objective.encode("utf-8")).hexdigest()[:12]
    return f"mission_{digest}"


class MissionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "mission_contract.v2"
    contract_id: str = ""
    objective: str = Field(min_length=1)
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    abort_conditions: list[MissionAbortCondition] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    evidence_requirements: list[str] = Field(default_factory=list)
    task_nodes: list[MissionTaskNode] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)
    risk_budget: MissionRiskBudgetPolicy | None = None
    capability_policy: MissionCapabilityPolicy | None = None
    memory_policy: MissionMemoryPolicy = Field(default_factory=MissionMemoryPolicy)
    recovery_policy: MissionRecoveryPolicy = Field(default_factory=MissionRecoveryPolicy)
    improvement_policy: MissionImprovementPolicy = Field(default_factory=MissionImprovementPolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version", "contract_id", "objective", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator(
        "allowed_actions",
        "forbidden_actions",
        "completion_criteria",
        "evidence_requirements",
        "success_metrics",
        mode="before",
    )
    @classmethod
    def _strip_text_list(cls, value: Any) -> list[str]:
        return _clean_text_list(value)

    @field_validator("abort_conditions", mode="before")
    @classmethod
    def _strip_abort_conditions(cls, value: Any) -> list[MissionAbortCondition]:
        return _clean_abort_conditions(value)

    @field_validator("task_nodes", mode="before")
    @classmethod
    def _strip_task_nodes(cls, value: Any) -> list[MissionTaskNode]:
        return _clean_task_nodes(value)

    @model_validator(mode="after")
    def _validate_task_graph(self) -> "MissionContract":
        node_ids = [node.node_id for node in self.task_nodes]
        duplicates = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
        if duplicates:
            raise ValueError(
                "mission task node ids must be unique: " + ", ".join(duplicates)
            )
        known_node_ids = set(node_ids)
        unknown_dependencies = sorted(
            {
                dependency
                for node in self.task_nodes
                for dependency in node.depends_on
                if dependency not in known_node_ids
            }
        )
        if unknown_dependencies:
            raise ValueError(
                "mission task node dependencies must reference known node ids: "
                + ", ".join(unknown_dependencies)
            )
        return self

    @property
    def abort_condition_types(self) -> set[MissionAbortConditionType]:
        return {condition.type for condition in self.abort_conditions}


def build_mission_contract(
    *,
    objective: str,
    constraints: list[str] | tuple[str, ...] | str | None = None,
    contract_id: str = "",
    allowed_actions: list[str] | tuple[str, ...] | str | None = None,
    forbidden_actions: list[str] | tuple[str, ...] | str | None = None,
    abort_conditions: list[Any] | tuple[Any, ...] | str | dict[str, Any] | None = None,
    completion_criteria: list[str] | tuple[str, ...] | str | None = None,
    evidence_requirements: list[str] | tuple[str, ...] | str | None = None,
    task_nodes: list[Any] | tuple[Any, ...] | dict[str, Any] | None = None,
    success_metrics: list[str] | tuple[str, ...] | str | None = None,
    risk_budget: dict[str, Any] | MissionRiskBudgetPolicy | None = None,
    capability_policy: dict[str, Any] | MissionCapabilityPolicy | None = None,
    memory_policy: dict[str, Any] | MissionMemoryPolicy | None = None,
    recovery_policy: dict[str, Any] | MissionRecoveryPolicy | None = None,
    improvement_policy: dict[str, Any] | MissionImprovementPolicy | None = None,
    metadata: dict[str, Any] | None = None,
) -> MissionContract:
    normalized_objective = str(objective or "").strip()
    if not normalized_objective:
        raise ValueError("mission contract objective is required")

    legacy_constraints = _clean_text_list(constraints)
    normalized_metadata = dict(metadata or {})
    if legacy_constraints and "constraints" not in normalized_metadata:
        normalized_metadata["constraints"] = legacy_constraints

    normalized_forbidden_actions = _clean_text_list(forbidden_actions)
    if not normalized_forbidden_actions:
        normalized_forbidden_actions = legacy_constraints

    return MissionContract(
        contract_id=str(contract_id or "").strip()
        or _default_contract_id(normalized_objective),
        objective=normalized_objective,
        allowed_actions=_clean_text_list(allowed_actions),
        forbidden_actions=normalized_forbidden_actions,
        abort_conditions=_clean_abort_conditions(abort_conditions)
        or _default_abort_conditions(),
        completion_criteria=_clean_text_list(completion_criteria)
        or list(_DEFAULT_COMPLETION_CRITERIA),
        evidence_requirements=_clean_text_list(evidence_requirements)
        or list(_DEFAULT_EVIDENCE_REQUIREMENTS),
        task_nodes=_clean_task_nodes(task_nodes),
        success_metrics=_clean_text_list(success_metrics),
        risk_budget=(
            MissionRiskBudgetPolicy.model_validate(risk_budget)
            if risk_budget is not None
            else None
        ),
        capability_policy=(
            MissionCapabilityPolicy.model_validate(capability_policy)
            if capability_policy is not None
            else None
        ),
        memory_policy=(
            MissionMemoryPolicy.model_validate(memory_policy)
            if memory_policy is not None
            else MissionMemoryPolicy()
        ),
        recovery_policy=(
            MissionRecoveryPolicy.model_validate(recovery_policy)
            if recovery_policy is not None
            else MissionRecoveryPolicy()
        ),
        improvement_policy=(
            MissionImprovementPolicy.model_validate(improvement_policy)
            if improvement_policy is not None
            else MissionImprovementPolicy()
        ),
        metadata=normalized_metadata,
    )


def normalize_mission_contract(
    mission_contract: MissionContract | dict[str, Any] | None,
    *,
    objective: str,
    constraints: list[str] | tuple[str, ...] | str | None = None,
    contract_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> MissionContract:
    if mission_contract is None:
        return build_mission_contract(
            objective=objective,
            constraints=constraints,
            contract_id=contract_id,
            metadata=metadata,
        )

    resolved = MissionContract.model_validate(mission_contract)
    updates: dict[str, Any] = {}
    if not resolved.contract_id:
        updates["contract_id"] = str(contract_id or "").strip() or _default_contract_id(
            resolved.objective
        )

    merged_metadata = dict(resolved.metadata)
    if metadata:
        merged_metadata.update(
            {key: value for key, value in metadata.items() if value not in (None, "")}
        )
    legacy_constraints = _clean_text_list(constraints)
    if legacy_constraints and "constraints" not in merged_metadata:
        merged_metadata["constraints"] = legacy_constraints
    if merged_metadata != resolved.metadata:
        updates["metadata"] = merged_metadata
    if legacy_constraints and not resolved.forbidden_actions:
        updates["forbidden_actions"] = legacy_constraints
    if not resolved.abort_conditions:
        updates["abort_conditions"] = _default_abort_conditions()
    if not resolved.completion_criteria:
        updates["completion_criteria"] = list(_DEFAULT_COMPLETION_CRITERIA)
    if not resolved.evidence_requirements:
        updates["evidence_requirements"] = list(_DEFAULT_EVIDENCE_REQUIREMENTS)

    if updates:
        resolved = resolved.model_copy(update=updates)
    return resolved
