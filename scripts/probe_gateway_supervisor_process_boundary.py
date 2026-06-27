#!/usr/bin/env python3
"""Exercise the Gateway supervisor process-probe boundary on loopback.

This is a C5b support smoke: it proves a real Gateway HTTP route can produce a
source-bound supervisor-process boundary artifact that later materialization can
match. It does not by itself claim full Gateway runtime, physical execution,
dispatch authority, or delivery completion.
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

from src.gateway.live_runtime_boundary import (
    GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_PATH,
)
from scripts.run_gateway_live_runtime_probe import (
    build_gateway_live_runtime_probe,
    build_gateway_live_runtime_probe_chain,
)


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
    source_path = output_dir / "gateway_supervisor_process_probe_source_runtime.json"
    _write_json(
        source_path,
        {
            "schema_version": "gateway_supervisor_process_probe_source_runtime.v1",
            "purpose": "supervisor_process_probe_boundary_only",
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
    gateway_supervisor_lifecycle_ref: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_runtime_artifact = (
        source_runtime_artifact
        if source_runtime_artifact is not None
        else _write_probe_source_runtime(output_dir)
    )
    source_runtime_payload = json.loads(source_runtime_artifact.read_text())
    source_runtime_artifact_ref = (
        source_runtime_payload.get("audit_id")
        if isinstance(source_runtime_payload, dict)
        else None
    )
    if not isinstance(source_runtime_artifact_ref, str) or not source_runtime_artifact_ref:
        source_runtime_artifact_ref = (
            "gateway_supervisor_process_probe_source:"
            f"{source_runtime_artifact.stem}"
        )
    with tempfile.TemporaryDirectory(prefix="gateway-supervisor-process-probe-") as tmp:
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
                "gateway_supervisor_lifecycle_ref": gateway_supervisor_lifecycle_ref,
                "source_runtime_artifact_ref": source_runtime_artifact_ref,
                "source_runtime_artifact_path": str(source_runtime_artifact),
                "source_runtime_artifact_sha256": _file_sha256(source_runtime_artifact),
            }
            async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                response = await client.post(
                    GATEWAY_SUPERVISOR_PROCESS_PROBE_BOUNDARY_PATH,
                    json=request_payload,
                )
                response.raise_for_status()
                artifact = response.json()
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10.0)

    artifact_path = output_dir / "gateway_supervisor_process_probe_boundary.json"
    artifact.update(
        {
            "gateway_supervisor_process_probe_boundary_artifact_path": str(
                artifact_path
            ),
            "loopback_gateway_base_url": base_url,
            "e2e_runtime_boundary_exercised": True,
            "environment_limitations": [
                "loopback Gateway supervisor process-probe boundary only",
                "no PX4/Gazebo SITL run was started",
                "no full Gateway-owned runtime was claimed",
            ],
        }
    )
    _write_json(artifact_path, artifact)
    return artifact


async def _run_materializer_exercise(
    *,
    output_dir: Path,
    source_runtime_artifact: Path,
) -> dict[str, Any]:
    """Exercise supervisor-boundary materialization and forged-ref blocking."""

    preflight_dir = output_dir / "preflight_chain"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    preflight = build_gateway_live_runtime_probe_chain(
        source_runtime_artifact_path=source_runtime_artifact,
        probe_dir=preflight_dir,
        live_probe_invoked=True,
    )
    gateway_session = json.loads(
        Path(preflight["gateway_mission_session_artifact_path"]).read_text()
    )
    lifecycle = json.loads(
        Path(preflight["gateway_supervisor_lifecycle_artifact_path"]).read_text()
    )
    boundary = await _run_probe(
        output_dir=output_dir,
        source_runtime_artifact=source_runtime_artifact,
        gateway_mission_session_ref=gateway_session["gateway_mission_session_ref"],
        supervisor_session_ref=gateway_session["supervisor_session_ref"],
        gateway_supervisor_lifecycle_ref=lifecycle["gateway_supervisor_lifecycle_ref"],
    )
    boundary_path = Path(boundary["gateway_supervisor_process_probe_boundary_artifact_path"])

    materialized = build_gateway_live_runtime_probe_chain(
        source_runtime_artifact_path=source_runtime_artifact,
        probe_dir=preflight_dir,
        live_probe_invoked=True,
        materialize_live_gateway_processes=True,
        gateway_supervisor_process_probe_boundary_artifact_path=boundary_path,
    )

    forged_dir = output_dir / "forged_chain"
    forged_dir.mkdir(parents=True, exist_ok=True)
    forged_preflight = build_gateway_live_runtime_probe_chain(
        source_runtime_artifact_path=source_runtime_artifact,
        probe_dir=forged_dir,
        live_probe_invoked=True,
    )
    forged_gateway_session = json.loads(
        Path(forged_preflight["gateway_mission_session_artifact_path"]).read_text()
    )
    forged_lifecycle = json.loads(
        Path(forged_preflight["gateway_supervisor_lifecycle_artifact_path"]).read_text()
    )
    forged_boundary = await _run_probe(
        output_dir=forged_dir,
        source_runtime_artifact=source_runtime_artifact,
        gateway_mission_session_ref=forged_gateway_session["gateway_mission_session_ref"],
        supervisor_session_ref=forged_gateway_session["supervisor_session_ref"],
        gateway_supervisor_lifecycle_ref=forged_lifecycle[
            "gateway_supervisor_lifecycle_ref"
        ],
    )
    forged_boundary["source_runtime_artifact_ref"] = "source_runtime:forged_wrong_ref"
    forged_boundary["gateway_supervisor_lifecycle_ref"] = (
        "gateway_supervisor_lifecycle:forged_wrong_ref"
    )
    forged_boundary_path = forged_dir / "forged_supervisor_process_probe_boundary.json"
    _write_json(forged_boundary_path, forged_boundary)
    forged = build_gateway_live_runtime_probe_chain(
        source_runtime_artifact_path=source_runtime_artifact,
        probe_dir=forged_dir,
        live_probe_invoked=True,
        materialize_live_gateway_processes=True,
        gateway_supervisor_process_probe_boundary_artifact_path=forged_boundary_path,
    )

    forged_socket_dir = output_dir / "forged_socket_chain"
    forged_socket_dir.mkdir(parents=True, exist_ok=True)
    forged_socket_preflight = build_gateway_live_runtime_probe_chain(
        source_runtime_artifact_path=source_runtime_artifact,
        probe_dir=forged_socket_dir,
        live_probe_invoked=True,
    )
    forged_socket_gateway_session = json.loads(
        Path(forged_socket_preflight["gateway_mission_session_artifact_path"]).read_text()
    )
    forged_socket_lifecycle = json.loads(
        Path(forged_socket_preflight["gateway_supervisor_lifecycle_artifact_path"]).read_text()
    )
    forged_socket_boundary = await _run_probe(
        output_dir=forged_socket_dir,
        source_runtime_artifact=source_runtime_artifact,
        gateway_mission_session_ref=forged_socket_gateway_session[
            "gateway_mission_session_ref"
        ],
        supervisor_session_ref=forged_socket_gateway_session["supervisor_session_ref"],
        gateway_supervisor_lifecycle_ref=forged_socket_lifecycle[
            "gateway_supervisor_lifecycle_ref"
        ],
    )
    forged_socket_boundary_path = Path(
        forged_socket_boundary["gateway_supervisor_process_probe_boundary_artifact_path"]
    )
    forged_socket_materialized = build_gateway_live_runtime_probe_chain(
        source_runtime_artifact_path=source_runtime_artifact,
        probe_dir=forged_socket_dir,
        live_probe_invoked=True,
        materialize_live_gateway_processes=True,
        gateway_supervisor_process_probe_boundary_artifact_path=(
            forged_socket_boundary_path
        ),
    )
    forged_socket_materializer_path = Path(
        forged_socket_materialized["gateway_live_process_materializer_artifact_path"]
    )
    forged_socket_observation_evidence_path = Path(
        forged_socket_materialized[
            "gateway_live_observation_process_evidence_artifact_path"
        ]
    )
    forged_socket_recovery_evidence_path = Path(
        forged_socket_materialized[
            "gateway_live_recovery_decision_process_evidence_artifact_path"
        ]
    )
    forged_socket_materializer = json.loads(
        forged_socket_materializer_path.read_text()
    )
    forged_socket_observation_evidence = json.loads(
        forged_socket_observation_evidence_path.read_text()
    )
    forged_socket_recovery_evidence = json.loads(
        forged_socket_recovery_evidence_path.read_text()
    )
    forged_socket_boundary_ref = "gateway_process_boundary:forged_socket_client_call"
    for payload in (
        forged_socket_materializer,
        forged_socket_observation_evidence,
        forged_socket_recovery_evidence,
    ):
        payload["gateway_process_boundary_kind"] = "gateway_socket_client_call"
        payload["gateway_process_boundary_ref"] = forged_socket_boundary_ref
    _write_json(forged_socket_materializer_path, forged_socket_materializer)
    _write_json(
        forged_socket_observation_evidence_path,
        forged_socket_observation_evidence,
    )
    _write_json(forged_socket_recovery_evidence_path, forged_socket_recovery_evidence)
    forged_socket = build_gateway_live_runtime_probe(
        source_runtime=json.loads(
            Path(forged_socket_materialized["source_runtime_artifact_path"]).read_text()
        ),
        source_runtime_artifact_path=source_runtime_artifact,
        gateway_session=forged_socket_gateway_session,
        gateway_session_artifact_path=Path(
            forged_socket_preflight["gateway_mission_session_artifact_path"]
        ),
        lifecycle=forged_socket_lifecycle,
        lifecycle_artifact_path=Path(
            forged_socket_preflight["gateway_supervisor_lifecycle_artifact_path"]
        ),
        observation_stream=json.loads(
            Path(
                forged_socket_materialized[
                    "gateway_owned_observation_stream_artifact_path"
                ]
            ).read_text()
        ),
        observation_stream_artifact_path=Path(
            forged_socket_materialized[
                "gateway_owned_observation_stream_artifact_path"
            ]
        ),
        recovery_loop=json.loads(
            Path(
                forged_socket_materialized[
                    "gateway_owned_recovery_decision_loop_artifact_path"
                ]
            ).read_text()
        ),
        recovery_loop_artifact_path=Path(
            forged_socket_materialized["gateway_owned_recovery_decision_loop_artifact_path"]
        ),
        readiness=json.loads(
            Path(forged_socket_preflight["gateway_full_runtime_readiness_artifact_path"]).read_text()
        ),
        readiness_artifact_path=Path(
            forged_socket_preflight["gateway_full_runtime_readiness_artifact_path"]
        ),
        live_probe_invoked=True,
        materializer=forged_socket_materializer,
        materializer_artifact_path=forged_socket_materializer_path,
    )
    forged_socket_path = forged_socket_dir / "gateway_live_runtime_probe_forged_socket.json"
    _write_json(forged_socket_path, forged_socket)

    summary = {
        "schema_version": "gateway_supervisor_process_probe_materializer_verification.v1",
        "verification_status": (
            "verified"
            if (
                materialized.get("gateway_runtime_probe_status")
                == "full_gateway_runtime_loop_observed"
                and materialized.get("full_gateway_runtime_loop") is True
                and forged.get("gateway_runtime_probe_status") == "blocked"
                and forged.get("full_gateway_runtime_loop") is False
                and forged_socket.get("gateway_runtime_probe_status") == "blocked"
                and forged_socket.get("full_gateway_runtime_loop") is False
            )
            else "blocked"
        ),
        "source_runtime_artifact_path": str(source_runtime_artifact),
        "gateway_supervisor_process_probe_boundary_artifact_path": str(boundary_path),
        "materialized_probe_artifact_path": materialized.get(
            "gateway_live_runtime_probe_artifact_path"
        ),
        "forged_boundary_artifact_path": str(forged_boundary_path),
        "forged_probe_artifact_path": forged.get(
            "gateway_live_runtime_probe_artifact_path"
        ),
        "forged_socket_probe_artifact_path": forged_socket.get(
            "gateway_live_runtime_probe_artifact_path"
        )
        or str(forged_socket_path),
        "materialized_status": materialized.get("gateway_runtime_probe_status"),
        "materialized_full_gateway_runtime_loop": materialized.get(
            "full_gateway_runtime_loop"
        ),
        "forged_status": forged.get("gateway_runtime_probe_status"),
        "forged_full_gateway_runtime_loop": forged.get("full_gateway_runtime_loop"),
        "forged_blocked_reasons": forged.get("blocked_reasons"),
        "forged_socket_status": forged_socket.get("gateway_runtime_probe_status"),
        "forged_socket_full_gateway_runtime_loop": forged_socket.get(
            "full_gateway_runtime_loop"
        ),
        "forged_socket_blocked_reasons": forged_socket.get("blocked_reasons"),
        "causal_form": "Form 0b",
        "progress_counted": False,
        "physical_execution_invoked": False,
        "hardware_target_allowed": False,
        "physical_form1_claimed": False,
        "dispatch_authority_created": False,
        "delivery_completion_claimed": False,
        "environment_limitations": [
            "materializer exercise used a supplied source runtime artifact",
            "no new PX4/Gazebo SITL run was started by this smoke",
            "verification covers C5b materializer promotion, forged-ref blocking, "
            "and forged socket-kind blocking",
        ],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    summary_path = output_dir / "gateway_supervisor_process_probe_materializer_verification.json"
    summary["summary_artifact_path"] = str(summary_path)
    _write_json(summary_path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe the Gateway supervisor process boundary."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/mission_designer_behavior_delta_audits")
        / f"gateway_supervisor_process_probe_boundary_{_utc_stamp()}",
    )
    parser.add_argument("--source-runtime-artifact", type=Path)
    parser.add_argument(
        "--exercise-c5b-materializer",
        action="store_true",
        help=(
            "After probing the Gateway route, feed the produced boundary into "
            "the C5b materializer and verify forged-ref blocking. Requires "
            "--source-runtime-artifact."
        ),
    )
    parser.add_argument(
        "--gateway-mission-session-ref",
        default="gateway_mission_session:supervisor_process_probe_boundary_probe",
    )
    parser.add_argument(
        "--supervisor-session-ref",
        default="gateway_supervisor_session:supervisor_process_probe_boundary_probe",
    )
    parser.add_argument(
        "--gateway-supervisor-lifecycle-ref",
        default="gateway_supervisor_lifecycle:supervisor_process_probe_boundary_probe",
    )
    args = parser.parse_args()

    if args.exercise_c5b_materializer:
        if args.source_runtime_artifact is None:
            raise SystemExit("--exercise-c5b-materializer requires --source-runtime-artifact")
        artifact = asyncio.run(
            _run_materializer_exercise(
                output_dir=args.output_dir,
                source_runtime_artifact=args.source_runtime_artifact,
            )
        )
    else:
        artifact = asyncio.run(
            _run_probe(
                output_dir=args.output_dir,
                source_runtime_artifact=args.source_runtime_artifact,
                gateway_mission_session_ref=args.gateway_mission_session_ref,
                supervisor_session_ref=args.supervisor_session_ref,
                gateway_supervisor_lifecycle_ref=args.gateway_supervisor_lifecycle_ref,
            )
        )
    print(json.dumps(artifact, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(artifact, sort_keys=True))
    return 0 if artifact.get("verification_status", "verified") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
