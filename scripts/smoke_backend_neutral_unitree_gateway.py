#!/usr/bin/env python3
"""Production Gateway smoke for the backend-neutral Unitree adapter runtime."""

from __future__ import annotations

import asyncio
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

from src.runtime.unitree_hardware_adapter import (
    UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
    UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV,
)
from src.runtime.unitree_mujoco_dispatch_bridge import (
    UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV,
    UNITREE_MUJOCO_BRIDGE_COMMAND_ENV,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_bridge(path: Path) -> None:
    path.write_text(
        "import json, sys\n"
        "request = json.loads(sys.stdin.read())\n"
        "action = request.get('action')\n"
        "response = {'physical_execution_invoked': False, "
        "'raw_lowcmd_published': False}\n"
        "if action == 'bounded_local_move':\n"
        "    response.update({'ack_status': 'accepted', "
        "'ack_source': 'unitree_subprocess_fixture'})\n"
        "elif action == 'read_state':\n"
        "    response.update({'pose_observed': True, 'base_stable': True})\n"
        "elif action == 'read_progress':\n"
        "    response.update({'runtime_progress_observed': True, "
        "'completion_observed': True, 'move_completed': True, "
        "'unitree_status': 'succeeded'})\n"
        "else:\n"
        "    response.update({'ack_status': 'accepted'})\n"
        "print(json.dumps(response))\n",
        encoding="utf-8",
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


def _request_payload(
    *,
    action_kind: str = "bounded_local_move",
    telemetry_fresh: bool = True,
    adapter_id: str = UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
    action_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "adapter_id": adapter_id,
        "missionos_action_ref": action_ref
        or f"unitree_gateway_smoke:{action_kind}",
        "action_kind": action_kind,
        "adapter_parameters": {"forward_m": 0.25, "lateral_m": 0.0},
        "execution_mode": "sim",
        "opt_in": True,
        "telemetry_fresh": telemetry_fresh,
        "heartbeat_alive": True,
        "geofence_satisfied": True,
        "operating_volume_satisfied": True,
    }


async def _run() -> dict[str, Any]:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        bridge = root / "unitree_bridge.py"
        _write_bridge(bridge)
        os.environ[UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV] = "1"
        os.environ[UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE_ENV] = "1"
        os.environ[UNITREE_MUJOCO_BRIDGE_COMMAND_ENV] = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(bridge))}"
        )
        os.environ["TASK_STORE_DB_PATH"] = str(root / "tasks.db")
        os.environ["MEMORY_DB_PATH"] = str(root / "memory.db")
        os.environ["AUDIT_LOG_PATH"] = str(root / "audit.log")
        os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(
            root / "computer_trajectories.db"
        )
        os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(
            root / "physical_ai_validation.db"
        )

        from src.config.settings import reset_settings
        from src.gateway.server import create_gateway
        from src.runtime.task_store import reset_task_store

        reset_settings()
        reset_task_store()
        gateway = create_gateway()
        task = gateway.task_store.create(
            kind="backend_neutral_adapter_smoke",
            title="Backend-neutral Unitree Gateway smoke",
            status="running",
            artifacts={},
        )
        task_id = task["task_id"]
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(
            uvicorn.Config(
                gateway.app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="on",
            )
        )
        server_task: asyncio.Task[Any] = asyncio.create_task(server.serve())
        try:
            await _wait_for_health(base_url)
            async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
                unregistered = await client.post(
                    "/missionos/hardware-adapters/prepare",
                    json={
                        "task_id": task_id,
                        "request": _request_payload(
                            adapter_id="unregistered_adapter.v1"
                        ),
                    },
                )
                stale = await client.post(
                    "/missionos/hardware-adapters/prepare",
                    json={
                        "task_id": task_id,
                        "request": _request_payload(telemetry_fresh=False),
                    },
                )
                missing_telemetry_request = _request_payload()
                missing_telemetry_request.pop("telemetry_fresh")
                missing_telemetry = await client.post(
                    "/missionos/hardware-adapters/prepare",
                    json={
                        "task_id": task_id,
                        "request": missing_telemetry_request,
                    },
                )
                unsupported = await client.post(
                    "/missionos/hardware-adapters/prepare",
                    json={
                        "task_id": task_id,
                        "request": _request_payload(action_kind="special_motion"),
                    },
                )
                prepared = await client.post(
                    "/missionos/hardware-adapters/prepare",
                    json={"task_id": task_id, "request": _request_payload()},
                )
                prepared.raise_for_status()
                preparation = prepared.json()["preparation"]
                rejected = await client.post(
                    "/missionos/hardware-adapters/approve-and-dispatch",
                    json={
                        "task_id": task_id,
                        "preparation_ref": preparation["preparation_ref"],
                        "preparation_sha256": preparation["preparation_sha256"],
                        "operator_approved": False,
                    },
                )
                dispatched = await client.post(
                    "/missionos/hardware-adapters/approve-and-dispatch",
                    json={
                        "task_id": task_id,
                        "preparation_ref": preparation["preparation_ref"],
                        "preparation_sha256": preparation["preparation_sha256"],
                        "operator_approved": True,
                    },
                )
                dispatched.raise_for_status()
                replay = await client.post(
                    "/missionos/hardware-adapters/approve-and-dispatch",
                    json={
                        "task_id": task_id,
                        "preparation_ref": preparation["preparation_ref"],
                        "preparation_sha256": preparation["preparation_sha256"],
                        "operator_approved": True,
                    },
                )
                repeated_prepare = await client.post(
                    "/missionos/hardware-adapters/prepare",
                    json={"task_id": task_id, "request": _request_payload()},
                )
                repeated_prepare.raise_for_status()
                repeated_dispatch = await client.post(
                    "/missionos/hardware-adapters/approve-and-dispatch",
                    json={
                        "task_id": task_id,
                        "preparation_ref": preparation["preparation_ref"],
                        "preparation_sha256": preparation["preparation_sha256"],
                        "operator_approved": True,
                    },
                )
                failure_prepared = await client.post(
                    "/missionos/hardware-adapters/prepare",
                    json={
                        "task_id": task_id,
                        "request": _request_payload(
                            action_ref="unitree_gateway_smoke:preflight_changed"
                        ),
                    },
                )
                failure_prepared.raise_for_status()
                failure_preparation = failure_prepared.json()["preparation"]
                os.environ[UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV] = "0"
                failed_dispatch = await client.post(
                    "/missionos/hardware-adapters/approve-and-dispatch",
                    json={
                        "task_id": task_id,
                        "preparation_ref": failure_preparation["preparation_ref"],
                        "preparation_sha256": failure_preparation[
                            "preparation_sha256"
                        ],
                        "operator_approved": True,
                    },
                )
                os.environ[UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV] = "1"
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=10.0)

        result = dispatched.json()["runtime_result"]
        verdict = result["verification_verdict"]
        approval = result["operator_approval"]
        invocations = result["runtime_invocation_evidence"]
        stale_preflight = stale.json()["preparation"]["preflight"]
        unsupported_preflight = unsupported.json()["preparation"]["preflight"]
        updated = gateway.task_store.get(task_id)
        artifacts = updated["artifacts"] if updated else {}
        failure_lifecycle = artifacts["missionos_hardware_adapter_preparations"][
            failure_preparation["preparation_ref"]
        ]["lifecycle_status"]

    summary = {
        "smoke": "backend_neutral_unitree_gateway",
        "task_id": task_id,
        "production_route": "/missionos/hardware-adapters",
        "adapter_id": UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        "unregistered_http_status": unregistered.status_code,
        "stale_preflight_status": stale_preflight["preflight_status"],
        "missing_telemetry_http_status": missing_telemetry.status_code,
        "unsupported_preflight_status": unsupported_preflight["preflight_status"],
        "missing_approval_http_status": rejected.status_code,
        "replay_http_status": replay.status_code,
        "repeated_prepare_reused": repeated_prepare.json()[
            "existing_preparation_reused"
        ],
        "repeated_dispatch_http_status": repeated_dispatch.status_code,
        "failed_dispatch_http_status": failed_dispatch.status_code,
        "failed_dispatch_lifecycle_status": failure_lifecycle,
        "dispatch_validation_status": dispatched.json()["dispatch_validation"][
            "validation_status"
        ],
        "dispatch_request_sent": result["dispatch_request_sent"],
        "command_ack_observed": result["command_ack_observed"],
        "runtime_state_observed": verdict["runtime_state_observed"],
        "runtime_progress_observed": result["runtime_progress_observed"],
        "runtime_invocation_count": len(result["runtime_invocation_evidence"]),
        "approval_preparation_bound": bool(
            approval["approved_preparation_ref"] == preparation["preparation_ref"]
            and approval["approved_preparation_sha256"]
            == preparation["preparation_sha256"]
            and approval["approval_expires_at"]
        ),
        "runtime_invocations_source_bound": all(
            item.get("missionos_adapter_id") == UNITREE_MUJOCO_HARDWARE_ADAPTER_ID
            and item.get("missionos_action_ref")
            == _request_payload()["missionos_action_ref"]
            and item.get("missionos_preparation_ref")
            == preparation["preparation_ref"]
            and item.get("missionos_preparation_sha256")
            == preparation["preparation_sha256"]
            and item.get("operator_approval_ref")
            == approval["operator_approval_ref"]
            for item in invocations
        ),
        "verification_status": verdict["verification_status"],
        "adapter_action_verified": verdict["adapter_action_verified"],
        "completion_claimed": result["completion_claimed"],
        "completion_scope": verdict["completion_scope"],
        "physical_execution_invoked": result["physical_execution_invoked"],
        "task_artifact_keys": sorted(artifacts),
        "limitations": [
            "subprocess fixture bridge; MuJoCo process not started",
            "sim_action only; no physical execution or mission completion",
        ],
    }
    assert summary["unregistered_http_status"] == 400
    assert summary["stale_preflight_status"] == "blocked"
    assert summary["missing_telemetry_http_status"] == 400
    assert summary["unsupported_preflight_status"] == "blocked"
    assert summary["missing_approval_http_status"] == 409
    assert summary["replay_http_status"] == 409
    assert summary["repeated_prepare_reused"] is True
    assert summary["repeated_dispatch_http_status"] == 409
    assert summary["failed_dispatch_http_status"] == 409
    assert summary["failed_dispatch_lifecycle_status"] == "failed"
    assert summary["dispatch_validation_status"] == "valid"
    assert summary["dispatch_request_sent"] is True
    assert summary["command_ack_observed"] is True
    assert summary["runtime_state_observed"] is True
    assert summary["runtime_progress_observed"] is True
    assert summary["runtime_invocation_count"] == 3
    assert summary["approval_preparation_bound"] is True
    assert summary["runtime_invocations_source_bound"] is True
    assert summary["verification_status"] == "verified"
    assert summary["adapter_action_verified"] is True
    assert summary["completion_claimed"] is True
    assert summary["completion_scope"] == "sim_action"
    assert summary["physical_execution_invoked"] is False
    return summary


def main() -> int:
    print(json.dumps(asyncio.run(_run()), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
