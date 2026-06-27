#!/usr/bin/env python3
"""Loopback smoke for MissionOS real-hardware arm/disarm Gateway route.

This smoke starts the real Gateway app on 127.0.0.1, posts to
``/missionos/real-hardware-arm-disarm-dispatch/run``, and verifies that the
path reaches planner -> authority -> dispatch validation -> executor. The real
serial executor gate is deliberately left off, so the route stops at
``blocked_at_executor`` and touches no hardware.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import socket
import sys
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import uvicorn

from src.intelligence.real_hardware_arm_disarm_planner import (
    REAL_HARDWARE_ARM_DISARM_PLANNER_ALLOW_OVERRIDE_ENV,
    REAL_HARDWARE_ARM_DISARM_PLANNER_COMMAND_ENV,
    REAL_HARDWARE_ARM_DISARM_RESPONSE_KIND,
)
from src.runtime.missionos_real_hardware_dispatch_runtime import (
    MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME_OPT_IN_ENV,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stub_planner_command() -> str:
    response = {
        "response_kind": REAL_HARDWARE_ARM_DISARM_RESPONSE_KIND,
        "parameters": {},
        "rationale": "Props removed; loopback smoke should request bounded approval.",
        "expected_outcome": "Gateway reaches the executor boundary without hardware.",
        "uncertainty": "Real serial link is unverified in this smoke.",
        "approval_request": "Operator approval required before any actuation.",
    }
    return (
        f"{sys.executable} -c "
        + shlex.quote(
            "import json,sys; sys.stdin.read(); print(json.dumps("
            + repr(response)
            + "))"
        )
    )


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(80):
            try:
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


async def _run() -> dict[str, Any]:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["TASK_STORE_DB_PATH"] = str(root / "tasks.db")
        os.environ["MEMORY_DB_PATH"] = str(root / "memory.db")
        os.environ["AUDIT_LOG_PATH"] = str(root / "audit.log")
        os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(
            root / "computer_trajectories.db"
        )
        os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(
            root / "physical_ai_validation.db"
        )
        os.environ[REAL_HARDWARE_ARM_DISARM_PLANNER_COMMAND_ENV] = (
            _stub_planner_command()
        )
        os.environ[REAL_HARDWARE_ARM_DISARM_PLANNER_ALLOW_OVERRIDE_ENV] = "1"
        os.environ.pop(MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME_OPT_IN_ENV, None)

        from src.config.settings import reset_settings
        from src.gateway.server import create_gateway
        from src.runtime.task_store import reset_task_store

        reset_settings()
        reset_task_store()
        gateway = create_gateway()
        task = gateway.task_store.create(
            kind="px4_real_hardware_actuator_dispatch",
            title="MissionOS real-hardware route loopback smoke",
            status="running",
            artifacts={
                "operational_safety_boundary": {
                    "target_kind": "px4_real_hardware_actuator",
                    "execution_source": "gateway_loopback_route_smoke",
                    "physical_execution_invoked": False,
                    "flight_execution_invoked": False,
                }
            },
        )

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(
            gateway.app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
        server = uvicorn.Server(config)
        server_task: asyncio.Task[Any] = asyncio.create_task(server.serve())
        try:
            await _wait_for_health(base_url)
            async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
                response = await client.post(
                    "/missionos/real-hardware-arm-disarm-dispatch/run",
                    json={
                        "task_id": task["task_id"],
                        "subject_id": "pixhawk-loopback-smoke",
                        "operator_approved": True,
                        "physical_attestation": {
                            "propellers_removed": True,
                            "operator_physically_present": True,
                            "attesting_operator_id": "operator-loopback-smoke",
                            "attested_at": datetime.now(timezone.utc).isoformat(),
                        },
                        "bench_context": {
                            "serial_device": "/dev/tty.usbmodem-loopback-smoke"
                        },
                        "operator_instruction": {
                            "text": "Prepare a props-removed arm/disarm bench dispatch."
                        },
                        "serial_device": "/dev/tty.usbmodem-loopback-smoke",
                        "opt_in": True,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=10.0)

        artifacts = gateway.task_store.get(task["task_id"])["artifacts"]

    orchestration = artifacts["missionos_real_hardware_dispatch_orchestration"][0]
    summary = {
        "smoke": "missionos_real_hardware_arm_disarm_route",
        "ran": True,
        "http_status": response.status_code,
        "task_id": task["task_id"],
        "orchestration_status": payload["orchestration_status"],
        "planner_status": payload["planner_result"]["planner_status"],
        "dispatch_validation_status": payload["dispatch_validation"][
            "validation_status"
        ],
        "backend_target": payload["dispatch_validation"]["backend_target"],
        "runtime_invoked": payload["runtime_invoked"],
        "blocked_reason": payload["runtime_result"]["blocked_reason"],
        "route_path": "/missionos/real-hardware-arm-disarm-dispatch/run",
        "agent_response_kind": orchestration["agent_proposed"]["response_kind"],
        "operator_approved": orchestration["human_approved"]["operator_approved"],
        "runtime_invocation_evidence_written": (
            "missionos_real_hardware_dispatch_runtime_invocations" in artifacts
        ),
    }

    assert summary["http_status"] == 200
    assert summary["orchestration_status"] == "blocked_at_executor"
    assert summary["planner_status"] == "proposal_guardrail_passed"
    assert summary["dispatch_validation_status"] == "valid"
    assert summary["backend_target"] == "px4_real_hardware"
    assert summary["runtime_invoked"] is False
    assert (
        summary["blocked_reason"]
        == f"{MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME_OPT_IN_ENV}_not_enabled"
    )
    assert summary["runtime_invocation_evidence_written"] is False
    return summary


def main() -> int:
    summary = asyncio.run(_run())
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
