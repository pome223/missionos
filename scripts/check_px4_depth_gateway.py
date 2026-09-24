"""Real CLI/HTTP depth-task smoke; --live additionally requires SITL opt-ins."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import secrets
import socket
import sys

import httpx
import uvicorn

from src.runtime import px4_depth_navigation as depth


async def check(args):
    root = args.output_dir.resolve()
    if root.exists():
        raise ValueError("use a fresh output directory")
    if args.live and any(os.getenv(k) != "1" for k in depth.OPT_INS):
        raise ValueError("live check requires all documented SITL opt-ins")
    root.mkdir(parents=True)
    for key, filename in (
        ("TASK_STORE_DB_PATH", "tasks.db"),
        ("MEMORY_DB_PATH", "memory.db"),
        ("AUDIT_LOG_PATH", "audit.log"),
        ("COMPUTER_TRAJECTORY_DB_PATH", "trajectory.db"),
        ("PHYSICAL_AI_VALIDATION_DB_PATH", "physical.db"),
    ):
        os.environ[key] = str(root / filename)
    os.environ["MISSIONOS_PX4_DEPTH_ARTIFACT_ROOT"] = str(root / "flights")
    os.environ["MISSIONOS_JEV_MODE"] = "off"
    os.environ["MISSIONOS_NAVIGATION_WAM_MODE"] = "off"
    if not args.live:
        for key in depth.OPT_INS:
            os.environ.pop(key, None)
    os.environ["GATEWAY_API_KEY"] = secrets.token_hex(24)
    from src.config.settings import reset_settings
    from src.runtime.task_store import reset_task_store
    from src.gateway.server import create_missionos_gateway

    reset_settings()
    reset_task_store()
    gateway = create_missionos_gateway()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(
            gateway.app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
        )
    )
    serving = asyncio.create_task(server.serve())

    async def cli(*command, expected=0):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "missionos_cli",
            "--gateway-url",
            url,
            "--json-output",
            "--state-path",
            str(root / "cli-state.json"),
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        (root / (command[0] + "-latest.stderr")).write_bytes(stderr)
        if process.returncode != expected:
            raise RuntimeError(
                f"CLI {command[0]} exited {process.returncode}: {stderr.decode()[-1200:]}"
            )
        return json.loads(stdout) if expected == 0 else stdout.decode()

    try:
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            for _ in range(100):
                with suppress(httpx.HTTPError):
                    if (await client.get("/health")).status_code == 200:
                        break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Gateway startup timed out")
            denied = await client.post(
                "/px4-gazebo/depth-navigation/prepare", json={"scene": "gap"}
            )
            assert denied.status_code == 401, denied.text
            headers = {"X-API-Key": os.environ["GATEWAY_API_KEY"]}
            rows = []
            for scene in (depth.SCENES if args.scene == "all" else (args.scene,)):
                proposed = await cli("prepare-px4-depth", "--scene", scene)
                task_id = proposed["task"]["task_id"]
                assert proposed["task"]["status"] == "pending"
                missing = await client.post(
                    "/px4-gazebo/mission-scenarios/execute-sitl",
                    headers=headers,
                    json={"task_id": task_id, "live_flight_mode": True},
                )
                assert missing.status_code == 400
                print(
                    json.dumps(
                        {"scene": scene, "task_prepared": True, "live": args.live}
                    ),
                    flush=True,
                )
                if args.live:
                    execution = await cli(
                        "execute-sitl", "--task-id", task_id, "--live-flight"
                    )
                    (root / f"{scene}-execution.json").write_text(
                        json.dumps(execution, indent=2) + "\n"
                    )
                    task = gateway.task_store.get(task_id)
                    result = task["artifacts"][depth.RESULT]
                    assert result["schema_version"] == "px4_depth_navigation_result.v1"
                    assert (
                        task["status"] == "completed"
                        and result["destination_reached"] is True
                    )
                    assert result["landing_and_disarm_observed"] is True
                    assert (
                        result["model_invoked"] is False
                        and result["simulator_removed"] is True
                    )
                    replay = await client.post(
                        "/px4-gazebo/mission-scenarios/execute-sitl",
                        headers=headers,
                        json={
                            "task_id": task_id,
                            "live_flight_mode": True,
                            "execution_approval_id": result["execution_approval_id"],
                        },
                    )
                    assert replay.status_code == 409
                    rows.append(
                        {
                            "scene": scene,
                            "task_id": task_id,
                            "result": result,
                            "replay_rejected": True,
                        }
                    )
                else:
                    approval = await client.post(
                        "/px4-gazebo/mission-scenarios/approve-sitl-execution",
                        headers=headers,
                        json={"task_id": task_id, "explicit_execution_approval": True},
                    )
                    assert approval.status_code == 200, approval.text
                    approval_id = approval.json()["execution_operator_approval"][
                        "approval_id"
                    ]
                    blocked = await client.post(
                        "/px4-gazebo/mission-scenarios/execute-sitl",
                        headers=headers,
                        json={
                            "task_id": task_id,
                            "live_flight_mode": True,
                            "execution_approval_id": approval_id,
                        },
                    )
                    assert blocked.status_code == 409 and "opt-in" in blocked.text
                    task = gateway.task_store.get(task_id)
                    assert (
                        task["status"] == "pending"
                        and depth.RESULT not in task["artifacts"]
                    )
                    assert not task["artifacts"][depth.APPROVALS][approval_id][
                        "consumed_in_runtime"
                    ]
                    rows.append(
                        {"scene": scene, "task_id": task_id, "opt_in_rejected": True}
                    )
                status = await cli("job-status", "--task-id", task_id)
                (root / f"{scene}-job-status.json").write_text(
                    json.dumps(status, indent=2) + "\n"
                )
                print(
                    json.dumps(
                        {"scene": scene, "verified": True, "status": task["status"]}
                    ),
                    flush=True,
                )
        result = {
            "check_passed": True,
            "scope": "live_px4_gateway" if args.live else "http_cli_no_flight",
            "unauthenticated_request_rejected": True,
            "missing_approval_rejected": True,
            "new_model_calls": 0,
            "new_rented_gpus": 0,
            "cases": rows,
        }
        (root / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps({"check_passed": True, "live": args.live, "cases": len(rows)}),
            flush=True,
        )
    finally:
        server.should_exit = True
        await serving


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scene", choices=(*depth.SCENES, "all"), default="climb")
    parser.add_argument("--live", action="store_true")
    asyncio.run(check(parser.parse_args()))
