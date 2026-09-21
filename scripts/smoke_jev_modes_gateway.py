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


async def run_mode(mode, root):
    root.mkdir()
    _configure_temp_paths(root)
    os.environ["MISSIONOS_JEV_MODE"] = mode
    os.environ["MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED"] = "1"
    os.environ["GATEWAY_API_KEY"] = ""
    reset_settings()
    reset_task_store()
    calls = {"primary": 0, "jev": 0}
    telemetry = _telemetry(sample_index=200, battery_percent=80.0)

    def primary(_self, prompt):
        calls["primary"] += 1
        return fixture._AssuranceJudge("replan").judge(prompt)

    def jev(_self, payload):
        calls["jev"] += 1
        return {
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

    with (
        patch.object(assurance._ADKJudge, "judge", primary),
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
                        "mission_context": {"execution_scope": "fixture"},
                    },
                )
                response.raise_for_status()
                graph = response.json()
        finally:
            server.should_exit = True
            await task
    proposal = graph["mission_assurance_proposal"]
    assert graph["graph_runtime_status"] == "proposal_guardrail_passed"
    expected = "hold" if mode == "primary" else "replan"
    assert proposal["proposed_response_kind"] == expected
    assert calls == {"primary": int(mode != "primary"), "jev": int(mode != "off")}
    evidence = proposal["model_invocation_evidence"]
    if mode == "shadow":
        assert evidence["jev_shadow"]["agrees_with_primary"] is False
        assert evidence["jev_shadow"]["used_for_decision"] is False
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
