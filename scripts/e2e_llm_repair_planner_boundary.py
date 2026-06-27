"""Runtime verification harness for the MissionOS Repair Planner boundary.

Starts the Gateway in-process, calls the production
/missionos/llm-repair-planner/run route, and asserts that a guarded Repair
Planner proposal includes the operator instruction and explicit parameters
needed by the chat UI. It also asserts that unsupported retry knobs such as wind
and mission upload timeout are blocked instead of becoming an approval card.
The harness uses the dev/test command override only when explicitly enabled by
this script; it does not approve, dispatch, or count progress.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/e2e_llm_repair_planner_boundary.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx
import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _repair_command(response: dict) -> str:
    return (
        f"{shlex_quote(sys.executable)} -c "
        f"{shlex_quote('import json; print(json.dumps(' + repr(response) + '))')}"
    )


def _payload_repair_response() -> dict:
    return {
        "repair_target": "adjust_form2a_response_parameters",
        "repair_actions": [
            {
                "action_type": "adjust_response_parameters",
                "description": "Prepare a bounded retry with a lighter payload.",
            }
        ],
        "rationale": "The latest blocked evidence needs a bounded retry proposal.",
        "expected_outcome": "A new prepared request that can be verified separately.",
        "uncertainty": "SITL may still fail; the verifier must decide.",
        "next_verification": "Run only through the explicit live SITL gate.",
        "proposed_operator_instruction": (
            "Repair Planner Agent proposes a bounded retry with payload 0.5kg."
        ),
        "proposed_parameters": {"payload_weight_kg": 0.5},
    }


def _unsupported_repair_response() -> dict:
    return {
        "repair_target": "adjust_form2a_response_parameters",
        "repair_actions": [
            {
                "action_type": "adjust_response_parameters",
                "description": "Increase upload timeout and change wind.",
            }
        ],
        "rationale": "This should be rejected by the repair boundary.",
        "expected_outcome": "No approval card should be created.",
        "uncertainty": "n/a",
        "next_verification": "Inspect guardrail blocking reasons.",
        "proposed_operator_instruction": "Increase upload timeout and change wind.",
        "proposed_parameters": {
            "mission_upload_timeout_seconds": 60,
            "wind_speed_mps": 1,
        },
    }


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


async def run_verification(tmp_path: Path) -> int:
    os.environ["TASK_STORE_DB_PATH"] = str(tmp_path / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(tmp_path / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(tmp_path / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(tmp_path / "computer_trajectories.db")
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(tmp_path / "physical_ai_validation.db")
    os.environ["MISSIONOS_ALLOW_LLM_REPAIR_PLANNER_COMMAND_OVERRIDE"] = "1"

    os.chdir(REPO_ROOT)

    from src.config.settings import reset_settings
    from src.gateway.server import create_gateway
    from src.runtime.task_store import reset_task_store

    reset_settings()
    reset_task_store()
    gateway = create_gateway()
    port = _free_port()
    config = uvicorn.Config(
        gateway.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    failures: list[str] = []

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=120.0) as client:
        for _ in range(100):
            try:
                response = await client.get("/health", timeout=0.3)
                if response.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.ReadTimeout):
                await asyncio.sleep(0.1)
        else:
            failures.append("Gateway health route did not become ready")

        if not failures:
            os.environ["MISSIONOS_LLM_REPAIR_PLANNER_COMMAND"] = _repair_command(
                _payload_repair_response()
            )
            response = await client.post("/missionos/llm-repair-planner/run")
            if response.status_code != 200:
                failures.append(f"HTTP status {response.status_code}, expected 200")
                data = {}
            else:
                data = response.json()

            proposal = data.get("repair_proposal") or {}
            authority = data.get("authority_boundary") or {}

            expected = {
                "summary_status": "repair_proposal_ready",
                "intelligence_source": "llm_repair_planner",
            }
            for key, expected_value in expected.items():
                actual = data.get(key) if key in data else proposal.get(key)
                if actual != expected_value:
                    failures.append(f"{key}={actual!r}, expected {expected_value!r}")

            classification = data.get("classification") or {}
            for key in ("progress_counted", "goal_640_progress_counted", "ai_agent_progress_counted"):
                if classification.get(key) is not False:
                    failures.append(f"classification.{key}={classification.get(key)!r}, expected False")
                if authority.get(key) is not False:
                    failures.append(f"authority_boundary.{key}={authority.get(key)!r}, expected False")

            if proposal.get("proposed_operator_instruction") != (
                "Repair Planner Agent proposes a bounded retry with payload 0.5kg."
            ):
                failures.append("proposed_operator_instruction was not preserved")

            if proposal.get("proposed_parameters") != {"payload_weight_kg": 0.5}:
                failures.append(f"proposed_parameters={proposal.get('proposed_parameters')!r}")

            if authority.get("blocking_reasons"):
                failures.append(f"unexpected blocking_reasons={authority.get('blocking_reasons')!r}")

            print(json.dumps({
                "case": "payload_repair_ready",
                "http_status": response.status_code,
                "summary_status": data.get("summary_status"),
                "intelligence_source": proposal.get("intelligence_source"),
                "proposed_operator_instruction": proposal.get("proposed_operator_instruction"),
                "proposed_parameters": proposal.get("proposed_parameters"),
                "classification": data.get("classification"),
                "blocking_reasons": authority.get("blocking_reasons"),
            }, indent=2, sort_keys=True))

        if not failures:
            os.environ["MISSIONOS_LLM_REPAIR_PLANNER_COMMAND"] = _repair_command(
                _unsupported_repair_response()
            )
            response = await client.post("/missionos/llm-repair-planner/run")
            if response.status_code != 200:
                failures.append(f"HTTP status {response.status_code}, expected 200 for unsupported case")
                data = {}
            else:
                data = response.json()
            authority = data.get("authority_boundary") or {}
            blocking_reasons = authority.get("blocking_reasons") or []
            expected_blocks = {
                "unsupported_operator_repair_parameter:mission_upload_timeout_seconds",
                "unsupported_operator_repair_parameter:wind_speed_mps",
            }
            if data.get("summary_status") != "blocked":
                failures.append(
                    f"unsupported summary_status={data.get('summary_status')!r}, expected 'blocked'"
                )
            missing_blocks = sorted(expected_blocks - set(blocking_reasons))
            if missing_blocks:
                failures.append(f"unsupported case missing blocking reasons={missing_blocks!r}")
            print(json.dumps({
                "case": "unsupported_repair_parameters_blocked",
                "http_status": response.status_code,
                "summary_status": data.get("summary_status"),
                "blocking_reasons": blocking_reasons,
                "classification": data.get("classification"),
            }, indent=2, sort_keys=True))

    server.should_exit = True
    await server_task

    if failures:
        print("FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PASSED: Repair Planner route returned a guarded proposal with explicit parameters.")
    return 0


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raise SystemExit(asyncio.run(run_verification(Path(tmp))))


if __name__ == "__main__":
    main()
