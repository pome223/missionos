"""Real loopback Gateway/ADK smoke for Jev mode selection; fixture model IO only."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import httpx
import uvicorn

from scripts import smoke_operator_chat_mission_incident_graph as fixture
from scripts.smoke_runtime_recovery_action_feasibility_gateway import (
    _configure_temp_paths,
    _telemetry,
)
from src.config.settings import reset_settings
from src.gateway import server as gateway_server
from src.intelligence import mission_assurance_agent as assurance
from src.intelligence.jev_assurance import JevAssuranceJudge
from src.runtime.task_store import reset_task_store


async def run_mode(
    mode, root, *, route="bounded", fast_path="disabled", failure=None, custom_mapping=False, declared=None,
    adk_assessment=None,
):
    root.mkdir()
    _configure_temp_paths(root)
    os.environ["MISSIONOS_JEV_MODE"] = mode
    os.environ["MISSIONOS_JEV_CASCADE_FAST_PATH"] = fast_path
    os.environ["MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED"] = "1"
    os.environ["GATEWAY_API_KEY"] = ""
    reset_settings()
    reset_task_store()
    calls = {"primary": 0, "jev": 0}
    telemetry = _telemetry(sample_index=200, battery_percent=80.0)

    original_request = JevAssuranceJudge._request
    original_adk_judge = assurance._ADKJudge.judge

    async def adk_response(_prompt):
        return json.dumps({
            "candidate_assessment": adk_assessment, "proposed_response_kind": "replan",
            "parameters": {}, "rationale": "Fixture candidate assessment.",
            "expected_outcome": "Proposal only.", "uncertainty": "Fixture.",
            "operator_question": "Review the evidence.",
        })

    def primary(_self, prompt):
        if failure == "reasoner_unconfigured":
            raise assurance.MissionAssuranceJudgeUnavailable("fixture_reasoner_not_enabled")
        calls["primary"] += 1
        if failure == "reasoner_timeout":
            raise TimeoutError("fixture_reasoner_timeout")
        if adk_assessment is not None:
            return original_adk_judge(_self, prompt)
        return fixture._AssuranceJudge("replan").judge(prompt)

    def jev(_self, payload):
        if failure == "jev_unconfigured":
            # Exercise the production missing-key check before any network access.
            return original_request(_self, payload)
        calls["jev"] += 1
        if failure == "jev_timeout":
            raise TimeoutError("fixture_jev_timeout")
        result = {
            "model": "fixture-jev",
            "answers": {
                "response": {
                    "type": "choice",
                    "choice": "hold",
                    "confidence": 0.9,
                    "probabilities": {
                        kind: float(kind == "hold")
                        for kind in payload["questions"]["response"]["criteria"]
                    },
                },
                "review": {"type": "choice", "choice": "bounded"},
            },
        }
        if "assessment_route" in payload["questions"]:
            result["answers"]["assessment_route"] = {
                "type": "choice", "choice": route, "confidence": 0.9,
                "probabilities": {
                    k: float(k == route)
                    for k in payload["questions"]["assessment_route"]["criteria"]
                },
            }
        if failure == "jev_malformed":
            result["answers"]["assessment_route"]["choice"] = "invalid"
        return result

    context = {"execution_scope": "fixture"}
    if declared:
        context["uncertainty"] = declared
    skip_jev = bool(declared)
    active_failure = failure if not (skip_jev and failure and failure.startswith("jev")) else None
    if custom_mapping:
        context["mission_contract"] = {"response_mapping": {"hold": "custom hold semantics"}}
    with (
        patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}),
        patch.object(assurance._ADKJudge, "judge", primary),
        patch.object(assurance, "_invoke_adk_response", adk_response),
        patch.object(JevAssuranceJudge, "_request", jev),
        patch.object(
            gateway_server,
            "run_missionos_runtime_recovery_agent",
            lambda **_: fixture._agent_result(telemetry),
        ),
    ):
        gateway = gateway_server.create_missionos_gateway()
        port = fixture._free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                gateway.app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
            )
        )
        task = asyncio.create_task(server.serve())
        try:
            await fixture._wait_for_health(f"http://127.0.0.1:{port}")
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                health = (await client.get("/health")).json()
                assert health["agent_runtime"]["mission_assurance"]["jev_mode"] == mode
                response = await client.post(
                    "/missionos/mission-incident/run",
                    json={
                        "telemetry_snapshot": telemetry,
                        "mission_context": context,
                    },
                )
                response.raise_for_status()
                graph = response.json()
        finally:
            server.should_exit = True
            await task
    proposal = graph["mission_assurance_proposal"]
    active_failure_status = "not_configured" if active_failure and active_failure.endswith("unconfigured") else "failed"
    expected_graph_status = "guardrail_blocked" if active_failure and mode == "cascade" else "proposal_guardrail_passed"
    assert graph["graph_runtime_status"] == expected_graph_status
    assert proposal["judgment_status"] == (active_failure_status if active_failure and mode == "cascade" else "proposal_guardrail_passed")
    if custom_mapping:
        assert graph["mission_situation"]["mission_contract"]["mission_context"]["response_mapping"]
    cascade_expected = (
        "operator_escalation" if route in {"need_observation", "human_review"}
        else "hold" if route == "bounded" and fast_path == "fixture_verified_detour_v1" and not custom_mapping
        else "replan"
    )
    if declared:
        cascade_expected = (
            "operator_escalation" if declared.get("required_observations_missing") or declared.get("operator_decision_required")
            else "replan"
        )
    if active_failure:
        cascade_expected = "operator_escalation"
    expected = "hold" if mode == "primary" else cascade_expected if mode == "cascade" else "replan"
    assert proposal["proposed_response_kind"] == expected
    expected_primary = int(mode != "primary")
    if mode == "cascade":
        expected_primary = int(cascade_expected == "replan")
    if active_failure in {"reasoner_timeout", "reasoner_semantics"}:
        expected_primary = 1
    elif active_failure == "reasoner_unconfigured":
        expected_primary = 0
    expected_jev = int(mode != "off" and active_failure != "jev_unconfigured" and not skip_jev)
    assert calls == {"primary": expected_primary, "jev": expected_jev}
    assert proposal["model_inference_invoked"] is bool(expected_primary or expected_jev)
    if active_failure and mode == "cascade":
        assert proposal["blocking_reasons"]
        assert graph["blocking_reasons"]
    else:
        assert proposal["blocking_reasons"] == []
    evidence = proposal["model_invocation_evidence"]
    if active_failure == "jev_malformed":
        candidate_evidence = evidence["jev_cascade_shadow"]["invocation_evidence"] if mode == "cascade_shadow" else evidence
        diagnostic = candidate_evidence["jev_cascade"]["jev_failure"]["invocation_evidence"]
        assert diagnostic["validation_reason"] == "invalid_jev_assessment_route"
        assert len(diagnostic["response_sha256"]) == 64
        assert diagnostic["raw_response_recorded"] is False
    if adk_assessment is not None:
        audit = evidence["jev_cascade"]
        if active_failure:
            assert audit["reasoner_failure"]["reason"] == "candidate_response_assessment_mismatch"
        else:
            assert audit["reasoner"]["invocation_evidence"]["candidate_semantics"]["assessment"] == "aligned"
    if mode == "shadow":
        assert evidence["jev_shadow"]["agrees_with_primary"] is False
        assert evidence["jev_shadow"]["used_for_decision"] is False
    elif mode == "cascade_shadow":
        shadow = evidence["jev_cascade_shadow"]
        assert shadow["used_for_decision"] is False
        if active_failure:
            assert shadow["status"] == active_failure_status
            assert shadow["output"] == {} and shadow["blocking_reasons"]
            assert shadow["model_inference_invoked"] is (active_failure != "jev_unconfigured")
        else:
            assert shadow["output"]["proposed_response_kind"] == cascade_expected
    elif mode == "cascade":
        assert evidence["jev_cascade"]["reasoner_invoked"] is bool(expected_primary)
        if custom_mapping:
            assert evidence["jev_cascade"]["route"] == "reasoner"
        if active_failure and active_failure.startswith("reasoner") and not skip_jev:
            assert evidence["jev_cascade"]["jev"]["invocation_evidence"]
    if declared:
        candidate_evidence = evidence["jev_cascade_shadow"]["invocation_evidence"] if mode == "cascade_shadow" else evidence
        assert candidate_evidence["jev_cascade"]["jev_invoked"] is False
        assert "jev" not in candidate_evidence["jev_cascade"]
        if mode == "cascade":
            assert proposal["judgment_mode"] == ("llm_required" if expected_primary or active_failure else "deterministic_routing")
    elif mode == "off":
        assert "jev_shadow" not in evidence
    for key in (
        "approval_created",
        "dispatch_authority_created",
        "executor_invoked",
        "physical_execution_invoked",
    ):
        assert graph[key] is False
    return {
        "mode": mode,
        "route_fixture": route,
        "fast_path": fast_path,
        "failure_fixture": failure,
        "custom_mapping": custom_mapping,
        "declared": declared,
        "adk_assessment": adk_assessment,
        "judgment_status": proposal["judgment_status"],
        "model_inference_invoked": proposal["model_inference_invoked"],
        "blocking_reasons": proposal["blocking_reasons"],
        "response": expected,
        "model_calls": calls,
        "graph_status": graph["graph_runtime_status"],
        "http_status": response.status_code,
        "authority_created": False,
    }


async def main():
    with tempfile.TemporaryDirectory(prefix="missionos-jev-mode-smoke-") as directory:
        results = [
            await run_mode(mode, Path(directory) / mode) for mode in ("off", "shadow", "primary")
        ]
        for mode in ("cascade", "cascade_shadow"):
            for route in ("bounded", "deep_reasoning", "need_observation", "human_review"):
                results.append(await run_mode(
                    mode, Path(directory) / (mode + route), route=route,
                    fast_path="fixture_verified_detour_v1",
                ))
        results.append(await run_mode("cascade", Path(directory) / "cascade_disabled"))
        for mode in ("cascade", "cascade_shadow"):
            for failure in ("jev_unconfigured", "jev_timeout", "jev_malformed"):
                results.append(await run_mode(mode, Path(directory) / (mode + failure), failure=failure))
            results.append(await run_mode(
                mode, Path(directory) / (mode + "custom_mapping"),
                fast_path="fixture_verified_detour_v1", custom_mapping=True,
            ))
        for failure in ("reasoner_unconfigured", "reasoner_timeout"):
            results.append(await run_mode("cascade", Path(directory) / failure, failure=failure))
        declarations = [
            {"required_observations_missing": ["occupancy"]},
            {"operator_decision_required": True},
            {"requires_additional_reasoning": True},
            {"required_observations_missing": ["occupancy"], "operator_decision_required": True,
             "requires_additional_reasoning": True},
        ]
        for mode in ("cascade", "cascade_shadow"):
            for index, declared in enumerate(declarations):
                results.append(await run_mode(
                    mode, Path(directory) / f"{mode}_declared_{index}",
                    declared=declared, failure="jev_unconfigured",
                ))
        for failure in ("reasoner_unconfigured", "reasoner_timeout"):
            results.append(await run_mode(
                "cascade", Path(directory) / (failure + "_direct"), failure=failure,
                declared={"requires_additional_reasoning": True},
            ))
        for assessment in ("aligned", "conflicting", "unresolved"):
            for direct in (False, True):
                results.append(await run_mode(
                    "cascade", Path(directory) / f"semantic_{assessment}_{direct}",
                    adk_assessment=assessment,
                    failure=None if assessment == "aligned" else "reasoner_semantics",
                    declared={"requires_additional_reasoning": True} if direct else None,
                ))
    print(
        json.dumps(
            {
                "runtime_boundary": "Gateway HTTP and ADK incident graph",
                "model_io": "fixture",
                "results": results,
                "external_model_calls": False,
                "physical_execution_invoked": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
