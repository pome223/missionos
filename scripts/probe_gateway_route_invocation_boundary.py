#!/usr/bin/env python3
"""Exercise the Gateway route-invocation process boundary on loopback.

This is a C5b support smoke: it proves a real Gateway HTTP route can produce a
source-bound process-boundary artifact. It does not claim full Gateway runtime,
physical execution, dispatch authority, or delivery completion.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
from typing import Any

import httpx
import uvicorn

from src.gateway.live_runtime_boundary import GATEWAY_ROUTE_INVOCATION_BOUNDARY_PATH


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _configure_temp_paths(tmp: Path) -> None:
    os.environ["TASK_STORE_DB_PATH"] = str(tmp / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(tmp / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(tmp / "computer_trajectories.db")
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(
        tmp / "physical_ai_validation.db"
    )


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(80):
            with suppress(httpx.HTTPError):
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


def _write_probe_source_runtime(output_dir: Path) -> Path:
    source_path = output_dir / "gateway_route_invocation_boundary_source_runtime.json"
    _write_json(
        source_path,
        {
            "schema_version": "gateway_route_invocation_boundary_source_runtime.v1",
            "purpose": "route_invocation_boundary_probe_only",
            "source_bound": True,
            "causal_form": "Form 0b",
            "progress_counted": False,
            "physical_execution_invoked": False,
            "hardware_target_allowed": False,
            "physical_form1_claimed": False,
            "dispatch_authority_created": False,
            "delivery_completion_claimed": False,
            "full_gateway_runtime_loop": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return source_path


async def _run_probe(
    *,
    output_dir: Path,
    source_runtime_artifact: Path | None,
    gateway_mission_session_ref: str,
    supervisor_session_ref: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_runtime_artifact = (
        source_runtime_artifact
        if source_runtime_artifact is not None
        else _write_probe_source_runtime(output_dir)
    )
    with tempfile.TemporaryDirectory(prefix="gateway-route-boundary-") as tmp:
        _configure_temp_paths(Path(tmp))

        from src.config.settings import reset_settings
        from src.gateway.server import create_gateway
        from src.runtime.task_store import reset_task_store

        reset_settings()
        reset_task_store()
        gateway = create_gateway()
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
        task = asyncio.create_task(server.serve())
        try:
            await _wait_for_health(base_url)
            request_payload = {
                "gateway_mission_session_ref": gateway_mission_session_ref,
                "supervisor_session_ref": supervisor_session_ref,
                "source_runtime_artifact_ref": (
                    "gateway_route_boundary_source:"
                    f"{source_runtime_artifact.stem}"
                ),
                "source_runtime_artifact_path": str(source_runtime_artifact),
                "source_runtime_artifact_sha256": _file_sha256(source_runtime_artifact),
            }
            async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                response = await client.post(
                    GATEWAY_ROUTE_INVOCATION_BOUNDARY_PATH,
                    json=request_payload,
                )
                response.raise_for_status()
                artifact = response.json()
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10.0)

    artifact_path = output_dir / "gateway_route_invocation_boundary.json"
    artifact.update(
        {
            "gateway_route_invocation_boundary_artifact_path": str(artifact_path),
            "loopback_gateway_base_url": base_url,
            "e2e_runtime_boundary_exercised": True,
            "environment_limitations": [
                "loopback Gateway route invocation only",
                "no PX4/Gazebo SITL run was started",
                "no Gateway-owned supervisor runtime was claimed",
            ],
        }
    )
    _write_json(artifact_path, artifact)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe the Gateway route-invocation process boundary."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits")
        / f"gateway_route_invocation_boundary_{_utc_stamp()}",
    )
    parser.add_argument("--source-runtime-artifact", type=Path)
    parser.add_argument(
        "--gateway-mission-session-ref",
        default="gateway_mission_session:route_invocation_boundary_probe",
    )
    parser.add_argument(
        "--supervisor-session-ref",
        default="gateway_supervisor_session:route_invocation_boundary_probe",
    )
    args = parser.parse_args()

    artifact = asyncio.run(
        _run_probe(
            output_dir=args.output_dir,
            source_runtime_artifact=args.source_runtime_artifact,
            gateway_mission_session_ref=args.gateway_mission_session_ref,
            supervisor_session_ref=args.supervisor_session_ref,
        )
    )
    print(json.dumps(artifact, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(artifact, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
