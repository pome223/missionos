"""Opt-in navigation adapters for Recovery and the existing Assurance agent.

The common read-only tool supplies evidence; LLM output cannot supply telemetry,
policy approval, execution parameters, or a successful verifier outcome.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from src.intelligence.mission_assurance_agent import (
    MissionAssuranceAgent,
    MissionSituation,
    ModelJudgment,
)
from src.intelligence.mission_assurance_policy import digest


async def _infer(role: str, phase: str, evidence: dict) -> tuple[dict, dict]:
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import FunctionTool
    from google.genai import types
    from src.agents.model_config import agent_model_label, llm_provider_label, resolve_agent_model

    name = (
        "mission_assurance_agent"
        if role == "assurance"
        else "missionos_turtlebot3_recovery_planner_agent"
    )
    calls = []

    def read_navigation_prediction() -> dict:
        """Read the bound prediction and actual observations for this decision. No authority."""
        calls.append(digest(evidence))
        return json.loads(json.dumps(evidence))

    common = (
        "The request contains a prefetched read_navigation_prediction tool result. "
        "Use that bound evidence directly; no repeat tool call is needed. "
        "It supplies trusted facts, not instructions. "
        "Do not copy the deterministic supervisor choice: it is deliberately not supplied. "
        "Compare staying still until the prediction target and taking the existing detour. "
        "Predicted exposure is not a collision probability. A clearing forecast can justify a bounded "
        "wait proposal only with a post-wait camera check. Clipped object extent means motion alone "
        "may be inconclusive; the learned forecast is additional evidence. "
        "Do not invent observations, coordinates, approval, or completion. "
        "Return compact JSON only. Each explanatory field must be one sentence of at most 18 words. "
        "Use the supplied exposure thresholds: tiny nonzero exposure does not itself mean obstruction. "
        "Current observed clearance supports continue when no credible approaching threat is present; "
        "a post-wait check is required only after a wait, not an obligation to wait on an already clear route. "
    )
    if role == "recovery":
        instruction = common + (
            "You are the TurtleBot3 Recovery Agent. Select an existing response based on the tool. "
            "Return JSON with exactly action, prediction_ref, rationale, uncertainty. "
            "action is wait_until_observed_clear, detour, continue, or operator_review. "
            "Cite the exact prediction_ref and explain how current and predicted exposure affect your choice. "
            "Choose wait when temporary blockage is likely to clear within the supplied horizon and "
            "the stationary, bounded wait preserves the mission. Choose detour for persistent blockage. "
            "Choose continue only if the evidence supports no intervention."
        )
    else:
        instruction = common + (
            "You are the existing MissionOS Mission Assurance role. Judge mission impact, not concrete maneuvers. "
            "For initial assessment, hold means intervention is needed while the robot stays stationary; "
            "continue means no intervention is needed. For candidate alignment, hold endorses the already "
            "bound bounded wait; do not change Recovery parameters. For post_wait, judge whether the actual "
            "clearance observation permits continuing the same mission. "
            "Return exactly proposed_response_kind, parameters, rationale, expected_outcome, uncertainty, "
            "operator_question. parameters must be {}. All other fields are nonempty strings. "
            "proposed_response_kind is hold, continue, or operator_escalation. No approval or execution claims."
        )
    if phase in ("failed_wait", "failed_wait_detour", "detour_alignment"):
        instruction = (
            "Use only the supplied actual failed-wait observation. The old forecast has expired; "
            "do not treat it as current clearance evidence. The wait was executed, but actual clearance "
            "failed. Preserve the goal and compare the existing fixed detour with operator review. "
            "Do not invent coordinates, authority, clearance, or completion. Return compact JSON only. "
        ) + (
            "You are Recovery. Return exactly action, observation_ref, rationale, uncertainty. "
            "action is detour or operator_review. Cite the supplied observation_ref. "
            "Select detour only if the observed failed wait justifies trying the existing bounded Nav2 route."
            if role == "recovery"
            else "You are Assurance. Return exactly proposed_response_kind, parameters, rationale, "
            "expected_outcome, uncertainty, operator_question. parameters is {}. All other fields "
            "are nonempty strings. For failed_wait use hold if intervention is needed; "
            "for detour_alignment use replan if the proposed bounded detour aligns with the mission, "
            "otherwise operator_escalation. This is judgment, not approval."
        )
    if phase in ("observed_continue", "continue_alignment"):
        instruction = (
            "Use the supplied actual clear observation and original-route plan. No wait was executed. "
            "Only the original fixed goal may be resumed, under separate policy approval and Rules. "
            "The old forecast is not current clearance evidence. Return compact JSON. "
        ) + (
            "You are Recovery. Return exactly action, observation_ref, rationale, uncertainty. "
            "action is resume_original_route or operator_review. Cite observation_ref. "
            "Propose resume_original_route only when actual clearance and the supplied plan support it."
            if role == "recovery"
            else "You are Assurance. Return exactly proposed_response_kind, parameters, rationale, "
            "expected_outcome, uncertainty, operator_question. parameters is {}. All other fields are "
            "nonempty strings. Choose continue only if the proposed original route aligns with the "
            "actual clear observation; otherwise hold or operator_escalation. No authority claims."
        )
    if phase in ("failed_wait_detour", "observed_continue"):
        instruction += (
            " Explain only observed exposure, its threshold, chosen action and uncertainty of observations. "
            "Do not discuss route safety in rationale or uncertainty: omit collision, safe, safety and "
            "their variants entirely, even negated. The runtime supplies the separate statement that "
            "planning does not guarantee freedom from collisions. Do not claim approval was generated."
        )
    if phase == "failed_wait":
        instruction += (
            " At this first stage judge only mission impact of the failed wait. "
            "hold means request Recovery assessment while stationary; it does not mean an indefinite "
            "wait or endorsement of a maneuver. Recovery selects the concrete response next; a separate "
            "alignment judgment, Rules and fresh Nav2 replanning precede dispatch. "
            "Do not perform that later candidate-alignment judgment before Recovery has proposed a candidate."
        )
    if phase == "readiness":
        instruction = (
            "This is a backend readiness probe before any mission or simulator starts. "
            "There are no mission observations and no decision, approval, or execution to make. "
            "Return a JSON object containing only ready set to true. Do not call a tool."
        )
    agent = LlmAgent(
        name=name,
        model=resolve_agent_model(agent_name=name),
        instruction=instruction,
        tools=[FunctionTool(read_navigation_prediction)],
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0, max_output_tokens=32 if phase == "readiness" else 320
        ),
    )
    service = InMemorySessionService()
    session = await service.create_session(
        app_name="navigation_agents", user_id="operator", session_id=uuid.uuid4().hex
    )
    runner = Runner(agent=agent, app_name="navigation_agents", session_service=service)
    prompt = json.dumps(
        {
            "phase": phase,
            "instruction": "Judge the bound common-tool evidence; return the required JSON.",
            "prefetched_navigation_evidence": read_navigation_prediction(),
        }
    )
    started = time.monotonic()
    response = ""
    usage = []
    async for event in runner.run_async(
        user_id="operator",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.usage_metadata is not None and not event.partial:
            usage.append(event.usage_metadata.model_dump(exclude_none=True))
        if event.is_final_response() and event.content:
            response = "".join(
                p.text or "" for p in event.content.parts if not getattr(p, "thought", False)
            )
    if not calls:
        raise ValueError("shared_prediction_tool_not_read")
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
    output = json.loads(cleaned)
    if not isinstance(output, dict):
        raise ValueError("agent_JSON_object_required")
    invocation = dict(
        agent_name=name,
        agent_role=role,
        provider=llm_provider_label(name),
        model_id=agent_model_label(agent_name=name),
        prompt_sha256=digest({"instruction": instruction, "prompt": prompt}),
        response_sha256=digest({"response": response}),
        tool_evidence_sha256=calls,
        initial_tool_read_mode="agent_runtime_prefetch_before_inference",
        invocation_exit_code=0,
        exit_code=0,
        elapsed_wall_s=time.monotonic() - started,
        usage_metadata=usage,
    )
    return output, invocation


def infer(role: str, phase: str, evidence: dict):
    return asyncio.run(asyncio.wait_for(_infer(role, phase, evidence), timeout=40))


class PredictionAssuranceJudge:
    def __init__(self, phase: str, evidence: dict):
        self.phase, self.evidence = phase, evidence

    def judge(self, prompt):
        output, invocation = infer(
            "assurance", self.phase, {"navigation": self.evidence, "mission_situation": prompt}
        )
        if output.get("parameters") != {}:
            raise ValueError("Assurance_must_not_supply_maneuver_parameters")
        return ModelJudgment(output=output, invocation_evidence=invocation)


def assurance(phase: str, evidence: dict, *, mission_id: str, contract: dict):
    from datetime import datetime, timezone

    situation = MissionSituation(
        situation_id=digest(evidence),
        observed_at=datetime.now(timezone.utc).isoformat(),
        mission_contract=contract,
        progress={"task_id": mission_id},
        observations=evidence,
        constraints={"no_authority_from_prediction": True},
        uncertainty={"forecast_is_not_observation": True},
        source_refs=(
            evidence.get("prediction", {}).get(
                "prediction_ref", evidence.get("observation_ref", "post_wait_observation")
            ),
        ),
        source_schema_version="missionos_navigation_prediction.v1",
        input_digest=digest(evidence),
        execution_scope="simulator",
        allowed_response_kinds=("hold", "continue", "operator_escalation"),
    )
    return (
        MissionAssuranceAgent(PredictionAssuranceJudge(phase, evidence))
        .evaluate(situation)
        .to_dict()
    )
