"""Backend-neutral Mission Assurance judgment.

The Agent receives a common mission situation and asks one configured LLM to
propose a semantic mission response.  It does not know backend verbs and never
creates approval, dispatch, execution, verification, progress, or completion
authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

MISSION_SITUATION_SCHEMA_VERSION = "missionos_mission_situation.v1"
MISSION_RESPONSE_PROPOSAL_SCHEMA_VERSION = "missionos_mission_response_proposal.v1"
MISSION_ASSURANCE_RESULT_SCHEMA_VERSION = "missionos_mission_assurance_agent_result.v1"

MISSION_ASSURANCE_ADK_ENABLED_ENV = "MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED"
MISSION_ASSURANCE_COMMAND_ENV = "MISSIONOS_MISSION_ASSURANCE_COMMAND"
MISSION_ASSURANCE_ALLOW_COMMAND_ENV = "MISSIONOS_ALLOW_MISSION_ASSURANCE_COMMAND_OVERRIDE"
MISSION_ASSURANCE_MODEL_ENV = "MISSIONOS_MISSION_ASSURANCE_MODEL_ID"
MISSION_ASSURANCE_TIMEOUT_ENV = "MISSIONOS_MISSION_ASSURANCE_TIMEOUT_SECONDS"
DEFAULT_MISSION_ASSURANCE_TIMEOUT_SECONDS = 60

MISSION_RESPONSE_KINDS = frozenset(
    {
        "continue",
        "hold",
        "replan",
        "return",
        "abort",
        "operator_escalation",
    }
)

class _MissionAssuranceADKResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposed_response_kind: Literal[
        "continue",
        "hold",
        "replan",
        "return",
        "abort",
        "operator_escalation",
    ]
    parameters: dict[str, Any]
    rationale: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    uncertainty: str = Field(min_length=1)
    operator_question: str = Field(min_length=1)


MISSION_ASSURANCE_RESPONSE_JSON_SCHEMA: dict[str, Any] = (
    _MissionAssuranceADKResponse.model_json_schema()
)

FORBIDDEN_MODEL_AUTHORITY_KEYS = frozenset(
    {
        "approved",
        "approval_granted",
        "operator_approved",
        "approval_recorded",
        "dispatch_authority_created",
        "dispatch_request_sent",
        "command_ack_observed",
        "runtime_progress_observed",
        "landing_observed",
        "delivery_completion_claimed",
        "physical_execution_invoked",
        "gate_passed",
        "progress_counted",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return _sha256_text(
        json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(values: Sequence[Any] | None) -> tuple[str, ...]:
    return tuple(str(value) for value in values or () if str(value).strip())


@dataclass(frozen=True)
class MissionSituation:
    """Common, source-bound facts presented to the mission-level judge."""

    situation_id: str
    observed_at: str
    mission_contract: Mapping[str, Any]
    progress: Mapping[str, Any]
    observations: Mapping[str, Any]
    constraints: Mapping[str, Any]
    uncertainty: Mapping[str, Any]
    source_refs: tuple[str, ...]
    source_schema_version: str
    input_digest: str
    execution_scope: str
    allowed_response_kinds: tuple[str, ...] = field(
        default_factory=lambda: tuple(sorted(MISSION_RESPONSE_KINDS))
    )
    schema_version: str = MISSION_SITUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.situation_id:
            raise ValueError("mission_situation_id_required")
        if not self.observed_at:
            raise ValueError("mission_situation_observed_at_required")
        if not self.input_digest:
            raise ValueError("mission_situation_input_digest_required")
        if not self.execution_scope:
            raise ValueError("mission_situation_execution_scope_required")
        allowed = set(self.allowed_response_kinds)
        if not allowed or not allowed.issubset(MISSION_RESPONSE_KINDS):
            raise ValueError("mission_situation_allowed_response_kinds_invalid")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MissionSituation:
        return cls(
            situation_id=str(value.get("situation_id") or ""),
            observed_at=str(value.get("observed_at") or ""),
            mission_contract=_mapping(value.get("mission_contract")),
            progress=_mapping(value.get("progress")),
            observations=_mapping(value.get("observations")),
            constraints=_mapping(value.get("constraints")),
            uncertainty=_mapping(value.get("uncertainty")),
            source_refs=_strings(value.get("source_refs")),
            source_schema_version=str(value.get("source_schema_version") or ""),
            input_digest=str(value.get("input_digest") or ""),
            execution_scope=str(value.get("execution_scope") or ""),
            allowed_response_kinds=(
                _strings(value.get("allowed_response_kinds"))
                or tuple(sorted(MISSION_RESPONSE_KINDS))
            ),
            schema_version=str(
                value.get("schema_version") or MISSION_SITUATION_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True)
class MissionResponseProposal:
    """An LLM proposal only; Rules and humans retain downstream authority."""

    proposal_id: str
    generated_at: str
    situation_ref: str
    situation_input_digest: str
    proposed_response_kind: str
    parameters: Mapping[str, Any]
    rationale: str
    expected_outcome: str
    uncertainty: str
    operator_question: str
    judgment_status: str
    judgment_mode: str
    fallback_mode: str
    model_inference_invoked: bool
    model_invocation_evidence: Mapping[str, Any]
    blocking_reasons: tuple[str, ...] = ()
    schema_version: str = MISSION_RESPONSE_PROPOSAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "operator_review_required": self.proposed_response_kind != "continue",
            "operator_approval_required": False,
            "llm_judgment_in_gate": False,
            "operator_approved": False,
            "approval_recorded": False,
            "dispatch_authority_created": False,
            "dispatch_request_sent": False,
            "command_ack_observed": False,
            "runtime_progress_observed": False,
            "landing_observed": False,
            "delivery_completion_claimed": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MissionResponseProposal:
        return cls(
            proposal_id=str(value.get("proposal_id") or ""),
            generated_at=str(value.get("generated_at") or ""),
            situation_ref=str(value.get("situation_ref") or ""),
            situation_input_digest=str(
                value.get("situation_input_digest") or ""
            ),
            proposed_response_kind=str(
                value.get("proposed_response_kind") or ""
            ),
            parameters=_mapping(value.get("parameters")),
            rationale=str(value.get("rationale") or ""),
            expected_outcome=str(value.get("expected_outcome") or ""),
            uncertainty=str(value.get("uncertainty") or ""),
            operator_question=str(value.get("operator_question") or ""),
            judgment_status=str(value.get("judgment_status") or ""),
            judgment_mode=str(value.get("judgment_mode") or ""),
            fallback_mode=str(value.get("fallback_mode") or ""),
            model_inference_invoked=(
                value.get("model_inference_invoked") is True
            ),
            model_invocation_evidence=_mapping(
                value.get("model_invocation_evidence")
            ),
            blocking_reasons=_strings(value.get("blocking_reasons")),
            schema_version=str(
                value.get("schema_version")
                or MISSION_RESPONSE_PROPOSAL_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True)
class ModelJudgment:
    output: Mapping[str, Any]
    invocation_evidence: Mapping[str, Any]


class MissionAssuranceJudge(Protocol):
    def judge(self, prompt: Mapping[str, Any]) -> ModelJudgment: ...


class MissionAssuranceJudgeUnavailable(RuntimeError):
    pass


def build_mission_assurance_prompt(situation: MissionSituation) -> dict[str, Any]:
    """Build a backend-neutral prompt; thresholds remain facts, not decisions."""

    response_semantics = {
        "continue": (
            "judge that no recovery action is needed and mission continuation is aligned"
        ),
        "hold": (
            "judge that the mission should remain held without dispatching the proposed action"
        ),
        "replan": (
            "judge that the Recovery Agent's feasible bounded reroute, obstacle avoidance, "
            "altitude, or speed proposal is mission-aligned; this does not approve or "
            "dispatch it"
        ),
        "return": (
            "judge that the Recovery Agent's feasible return proposal is mission-aligned; "
            "this does not approve or dispatch it"
        ),
        "abort": (
            "judge that the Recovery Agent's feasible abort or land proposal is "
            "mission-aligned; this does not approve or dispatch it"
        ),
        "operator_escalation": (
            "use when mission-level evidence is insufficient or contradictory, not merely "
            "because a separate human approval is required downstream"
        ),
    }
    return {
        "schema_version": "missionos_mission_assurance_prompt.v1",
        "task": "judge_mission_continuation_response",
        "role_contract": {
            "llm_judges": True,
            "recovery_agent_proposal_is_input_not_approval": True,
            "human_approves": True,
            "rules_constrain": True,
            "executor_acts": True,
            "verifier_checks": True,
            "repair_is_separately_bound": True,
        },
        "decision_contract": {
            "thresholds_are_inputs_not_final_judgment": True,
            "judge_recovery_proposal_mission_alignment": True,
            "response_kind_is_judgment_not_execution_authority": True,
            "human_approval_is_always_a_separate_downstream_boundary": True,
            "do_not_choose_operator_escalation_only_because_human_approval_is_required": True,
            "do_not_generate_or_modify_backend_action": True,
            "choose_exactly_one_response": True,
            "allowed_response_kinds": list(situation.allowed_response_kinds),
            "required_output_fields": [
                "proposed_response_kind",
                "parameters",
                "rationale",
                "expected_outcome",
                "uncertainty",
                "operator_question",
            ],
            "forbidden_authority_keys": sorted(FORBIDDEN_MODEL_AUTHORITY_KEYS),
        },
        "response_semantics": {
            response_kind: response_semantics[response_kind]
            for response_kind in situation.allowed_response_kinds
        },
        "mission_situation": situation.to_dict(),
    }


class MissionAssuranceAgent:
    """One mission-level LLM judgment path shared by every environment adapter."""

    def __init__(self, judge: MissionAssuranceJudge) -> None:
        self._judge = judge

    def evaluate(self, situation: MissionSituation) -> MissionResponseProposal:
        prompt = build_mission_assurance_prompt(situation)
        try:
            judgment = self._judge.judge(prompt)
        except MissionAssuranceJudgeUnavailable as exc:
            return self._escalation(
                situation,
                status="not_configured",
                invoked=False,
                reasons=(str(exc) or "mission_assurance_judge_not_configured",),
            )
        except Exception as exc:  # noqa: BLE001 - model providers fail heterogeneously.
            return self._escalation(
                situation,
                status="failed",
                invoked=True,
                reasons=(f"mission_assurance_judge_failed:{type(exc).__name__}",),
            )

        output = _mapping(judgment.output)
        reasons = self._validation_reasons(output, situation=situation)
        if reasons:
            return self._escalation(
                situation,
                status="guardrail_blocked",
                invoked=True,
                reasons=tuple(reasons),
                invocation_evidence=judgment.invocation_evidence,
            )
        return MissionResponseProposal(
            proposal_id=f"mission_response_proposal_{uuid.uuid4().hex[:12]}",
            generated_at=_utc_now(),
            situation_ref=f"mission_situation:{situation.situation_id}",
            situation_input_digest=situation.input_digest,
            proposed_response_kind=str(output["proposed_response_kind"]),
            parameters=_mapping(output.get("parameters")),
            rationale=str(output["rationale"]),
            expected_outcome=str(output["expected_outcome"]),
            uncertainty=str(output["uncertainty"]),
            operator_question=str(output["operator_question"]),
            judgment_status="proposal_guardrail_passed",
            judgment_mode="llm_required",
            fallback_mode="operator_escalation_only",
            model_inference_invoked=True,
            model_invocation_evidence=dict(judgment.invocation_evidence),
        )

    @staticmethod
    def _validation_reasons(
        output: Mapping[str, Any], *, situation: MissionSituation
    ) -> list[str]:
        reasons = [
            f"raw_llm_output_forbidden_authority_key:{key}"
            for key in sorted(FORBIDDEN_MODEL_AUTHORITY_KEYS)
            if key in output
        ]
        response_kind = str(output.get("proposed_response_kind") or "")
        if response_kind not in set(situation.allowed_response_kinds):
            reasons.append("proposed_response_kind_not_allowed")
        if not isinstance(output.get("parameters"), Mapping):
            reasons.append("parameters_mapping_required")
        for key in (
            "rationale",
            "expected_outcome",
            "uncertainty",
            "operator_question",
        ):
            if not isinstance(output.get(key), str) or not output.get(key):
                reasons.append(f"{key}_required")
        return reasons

    @staticmethod
    def _escalation(
        situation: MissionSituation,
        *,
        status: str,
        invoked: bool,
        reasons: tuple[str, ...],
        invocation_evidence: Mapping[str, Any] | None = None,
    ) -> MissionResponseProposal:
        return MissionResponseProposal(
            proposal_id=f"mission_response_proposal_{uuid.uuid4().hex[:12]}",
            generated_at=_utc_now(),
            situation_ref=f"mission_situation:{situation.situation_id}",
            situation_input_digest=situation.input_digest,
            proposed_response_kind="operator_escalation",
            parameters={},
            rationale="Mission-level judgment was not safely available.",
            expected_outcome="A human reviews the source-bound situation without an action offer.",
            uncertainty="No action judgment is accepted from the failed or unavailable model path.",
            operator_question="Review the mission situation and choose the next step.",
            judgment_status=status,
            judgment_mode="llm_required",
            fallback_mode="operator_escalation_only",
            model_inference_invoked=invoked,
            model_invocation_evidence=dict(invocation_evidence or {}),
            blocking_reasons=reasons,
        )


class _UnavailableJudge:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def judge(self, prompt: Mapping[str, Any]) -> ModelJudgment:
        _ = prompt
        raise MissionAssuranceJudgeUnavailable(self._reason)


class _CommandJudge:
    def __init__(self, command_text: str) -> None:
        self._command = shlex.split(command_text)

    def judge(self, prompt: Mapping[str, Any]) -> ModelJudgment:
        prompt_text = json.dumps(dict(prompt), sort_keys=True)
        started_at = _utc_now()
        process = subprocess.run(
            self._command,
            input=prompt_text,
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            check=False,
        )
        completed_at = _utc_now()
        if process.returncode != 0:
            raise RuntimeError("mission_assurance_command_exit_code_nonzero")
        try:
            output = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("mission_assurance_command_stdout_not_json") from exc
        if not isinstance(output, Mapping):
            raise TypeError("mission_assurance_command_stdout_not_object")
        return ModelJudgment(
            output=dict(output),
            invocation_evidence={
                "invocation_kind": "subprocess",
                "model_id": os.environ.get(
                    MISSION_ASSURANCE_MODEL_ENV, "command_override"
                ),
                "prompt_sha256": _sha256_text(prompt_text),
                "response_sha256": _sha256_text(process.stdout),
                "started_at": started_at,
                "completed_at": completed_at,
                "exit_code": process.returncode,
                "stderr_sha256": _sha256_text(process.stderr),
            },
        )


class _ADKJudge:
    def judge(self, prompt: Mapping[str, Any]) -> ModelJudgment:
        from src.agents.model_config import llm_provider_label

        prompt_text = json.dumps(dict(prompt), sort_keys=True)
        started_at = _utc_now()
        response_text = asyncio.run(
            asyncio.wait_for(
                _invoke_adk_response(prompt_text), timeout=_timeout_seconds()
            )
        )
        completed_at = _utc_now()
        try:
            output = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise ValueError("mission_assurance_adk_response_not_json") from exc
        if not isinstance(output, Mapping):
            raise TypeError("mission_assurance_adk_response_not_object")
        return ModelJudgment(
            output=dict(output),
            invocation_evidence={
                "invocation_kind": "adk_llm",
                "provider": llm_provider_label("mission_assurance_agent"),
                "model_id": _model_id(),
                "prompt_sha256": _sha256_text(prompt_text),
                "response_sha256": _sha256_text(response_text),
                "started_at": started_at,
                "completed_at": completed_at,
                "exit_code": 0,
            },
        )


def _adk_output_schema() -> type[_MissionAssuranceADKResponse] | None:
    """Avoid DeepSeek response_format while retaining guarded JSON parsing."""

    from src.agents.model_config import deepseek_llm_backend_enabled

    if deepseek_llm_backend_enabled("mission_assurance_agent"):
        return None
    return _MissionAssuranceADKResponse


async def _invoke_adk_response(prompt_text: str) -> str:
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.genai import types

    from src.agents.model_config import resolve_agent_model
    from src.runtime.session_service import create_session_service

    _configure_adk_environment()
    agent_kwargs: dict[str, Any] = {
        "name": "mission_assurance_agent",
        "model": resolve_agent_model(
            _model_id(), agent_name="mission_assurance_agent"
        ),
        "instruction": (
            "You are the backend-neutral MissionOS Mission Assurance Agent. "
            "The Runtime Recovery Agent has already proposed a concrete recovery "
            "action inside the MissionSituation. Judge whether that proposal is "
            "aligned with the wider mission: continue, hold, replan, return, "
            "abort, or operator escalation. Do not generate, replace, or modify "
            "the recovery action or its parameters. A supported "
            "return_to_launch proposal maps to the semantic response return with "
            "an empty parameters object. Action Feasibility and operator "
            "preapproval are execution constraints, not proof of mission "
            "alignment and not instructions to select the proposed action. If "
            "the proposal conflicts with an active mission objective or "
            "constraint and no source-backed necessity overrides that conflict, "
            "select the appropriate continue, hold, replan, or operator_escalation "
            "response. Use hold when a source-backed safe pause preserves the "
            "mission while awaiting an observation or operator decision. Use "
            "operator_escalation when the evidence is insufficient or conflicting "
            "so that no bounded response can be judged. Return one JSON object only. "
            "Return exactly these six keys and omit none: "
            "proposed_response_kind, parameters, rationale, expected_outcome, "
            "uncertainty, operator_question. parameters must be an object and "
            "the other five values must be non-empty strings. "
            "Treat thresholds as evidence and constraints, not as the final "
            "decision. Never approve, dispatch, execute, verify, or claim progress."
        ),
        "generate_content_config": types.GenerateContentConfig(temperature=0.0),
    }
    output_schema = _adk_output_schema()
    if output_schema is not None:
        agent_kwargs["output_schema"] = output_schema
    agent = LlmAgent(**agent_kwargs)
    app_name = "missionos_mission_assurance"
    user_id = "missionos_operator"
    service = create_session_service()
    session = await service.create_session(app_name=app_name, user_id=user_id)
    runner = Runner(agent=agent, app_name=app_name, session_service=service)
    content = types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    "Judge this mission situation as JSON only. Use exactly "
                    'this shape: {"proposed_response_kind":"return",'
                    '"parameters":{},"rationale":"...",'
                    '"expected_outcome":"...","uncertainty":"...",'
                    '"operator_question":"..."}. Choose the response kind '
                    "from the supplied allowed list; the example value is not "
                    f"an instruction. Mission situation:\n{prompt_text}"
                )
            )
        ],
    )
    parts: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=content,
    ):
        if not event.is_final_response() or not event.content:
            continue
        for part in event.content.parts or []:
            value = getattr(part, "text", None)
            if value:
                parts.append(value)
    return "".join(parts).strip()


def _model_id() -> str:
    from src.agents.model_config import agent_model_label

    return agent_model_label(
        os.environ.get(MISSION_ASSURANCE_MODEL_ENV, "") or None,
        agent_name="mission_assurance_agent",
    )


def _configure_adk_environment() -> None:
    from src.agents.model_config import (
        configure_google_vertex_location,
        google_llm_backend_enabled,
    )

    if not google_llm_backend_enabled("mission_assurance_agent"):
        return
    try:
        from src.config.settings import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 - configuration backends vary by install.
        return
    api_key = str(getattr(settings, "google_api_key", "") or "").strip()
    if api_key and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = api_key
    if not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
        use_vertex = bool(getattr(settings, "google_genai_use_vertexai", False))
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = (
            "true" if use_vertex else "false"
        )
    configure_google_vertex_location(
        _model_id(), agent_name="mission_assurance_agent"
    )


def _timeout_seconds() -> int:
    try:
        return max(
            1,
            int(
                os.environ.get(
                    MISSION_ASSURANCE_TIMEOUT_ENV,
                    DEFAULT_MISSION_ASSURANCE_TIMEOUT_SECONDS,
                )
            ),
        )
    except ValueError:
        return DEFAULT_MISSION_ASSURANCE_TIMEOUT_SECONDS


def configured_mission_assurance_agent() -> MissionAssuranceAgent:
    """Return the single configured Agent; unavailable LLMs escalate only."""

    if os.environ.get(MISSION_ASSURANCE_ADK_ENABLED_ENV) == "1":
        return MissionAssuranceAgent(_ADKJudge())
    command_text = os.environ.get(MISSION_ASSURANCE_COMMAND_ENV, "").strip()
    if command_text:
        if os.environ.get(MISSION_ASSURANCE_ALLOW_COMMAND_ENV) != "1":
            return MissionAssuranceAgent(
                _UnavailableJudge(f"{MISSION_ASSURANCE_ALLOW_COMMAND_ENV}_required")
            )
        return MissionAssuranceAgent(_CommandJudge(command_text))
    return MissionAssuranceAgent(
        _UnavailableJudge(f"{MISSION_ASSURANCE_ADK_ENABLED_ENV}_not_enabled")
    )


def persist_mission_assurance_evaluation(
    *,
    situation: MissionSituation,
    proposal: MissionResponseProposal,
    artifact_root: Path | str,
    artifact_relative: Callable[[Path], str],
) -> dict[str, Any]:
    """Persist source-bound judgment artifacts without adding authority."""

    root = Path(artifact_root)
    situation_payload = situation.to_dict()
    situation_dir = root / "missionos_mission_situation" / situation.situation_id
    situation_dir.mkdir(parents=True, exist_ok=True)
    situation_path = situation_dir / "missionos_mission_situation.json"
    situation_path.write_text(
        json.dumps(situation_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    proposal_payload = proposal.to_dict()
    proposal_dir = root / "missionos_mission_response_proposal" / proposal.proposal_id
    proposal_dir.mkdir(parents=True, exist_ok=True)
    proposal_path = proposal_dir / "missionos_mission_response_proposal.json"
    proposal_path.write_text(
        json.dumps(proposal_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": MISSION_ASSURANCE_RESULT_SCHEMA_VERSION,
        "agent_status": proposal.judgment_status,
        "situation": situation_payload,
        "situation_ref": f"mission_situation:{situation.situation_id}",
        "situation_artifact_path": artifact_relative(situation_path),
        "situation_sha256": _canonical_sha256(situation_payload),
        "proposal": proposal_payload,
        "proposal_ref": f"mission_response_proposal:{proposal.proposal_id}",
        "proposal_artifact_path": artifact_relative(proposal_path),
        "proposal_sha256": _canonical_sha256(proposal_payload),
        "llm_judgment_in_gate": False,
        "operator_approved": False,
        "dispatch_authority_created": False,
        "dispatch_request_sent": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


__all__ = [
    "MISSION_ASSURANCE_ADK_ENABLED_ENV",
    "MISSION_ASSURANCE_ALLOW_COMMAND_ENV",
    "MISSION_ASSURANCE_COMMAND_ENV",
    "MISSION_ASSURANCE_RESULT_SCHEMA_VERSION",
    "MISSION_RESPONSE_KINDS",
    "MISSION_RESPONSE_PROPOSAL_SCHEMA_VERSION",
    "MISSION_SITUATION_SCHEMA_VERSION",
    "MissionAssuranceAgent",
    "MissionAssuranceJudge",
    "MissionAssuranceJudgeUnavailable",
    "MissionResponseProposal",
    "MissionSituation",
    "ModelJudgment",
    "build_mission_assurance_prompt",
    "configured_mission_assurance_agent",
    "persist_mission_assurance_evaluation",
]
