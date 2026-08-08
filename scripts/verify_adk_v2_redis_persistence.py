#!/usr/bin/env python3
"""Verify ADK v2 event persistence across independent Redis client processes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
import uuid

from google.adk.events import Event, EventActions
from google.adk.events.event import NodeInfo

from src.runtime.session_service import (
    RedisSessionService,
    create_session_service,
    describe_session_backend,
)


APP_NAME = "missionos_adk_v2_redis_persistence"
USER_ID = "missionos_redis_persistence_verifier"
EVENT_ID = "event_adk_v2_node_output_fixture"
NODE_PATH = "missionos_conversation_proposal_v2.invoke_shadow_safety_critic"


def _settings() -> SimpleNamespace:
    redis_url = os.environ.get("REDIS_URL", "").strip()
    namespace = os.environ.get(
        "REDIS_SESSION_NAMESPACE",
        "missionos:adk-v2:persistence-smoke",
    ).strip()
    return SimpleNamespace(
        redis_url=redis_url,
        redis_session_namespace=namespace,
    )


def _service() -> tuple[RedisSessionService, dict[str, Any]]:
    settings = _settings()
    descriptor = describe_session_backend(settings)
    if not settings.redis_url:
        raise RuntimeError("redis_persistence_smoke_requires_REDIS_URL")
    if descriptor.get("backend") != "redis":
        raise RuntimeError("redis_persistence_smoke_backend_not_redis")
    service = create_session_service(settings, require_redis=True)
    if not isinstance(service, RedisSessionService):
        raise RuntimeError("redis_persistence_smoke_service_not_redis")
    return service, descriptor


async def _close_service(service: RedisSessionService) -> None:
    close = getattr(service._client, "aclose", None)
    if callable(close):
        await close()


def _event(*, task_id: str) -> Event:
    return Event(
        id=EVENT_ID,
        invocation_id=f"invocation_{task_id}",
        author="missionos_adk_v2_graph",
        node_info=NodeInfo(
            path=NODE_PATH,
            output_for=["finalize_shadow_proposal"],
            message_as_output=False,
        ),
        output={
            "schema_version": "missionos_adk_v2_persisted_node_output.v1",
            "task_id": task_id,
            "mission_response_candidate_ref": f"mission_response_candidate:{task_id}",
            "approval_created": False,
            "dispatch_authority_created": False,
            "executor_invoked": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        },
        custom_metadata={
            "missionos_correlation": {
                "task_id": task_id,
                "workflow_name": "missionos_conversation_proposal_shadow_v2",
                "node_path": NODE_PATH,
            }
        },
        actions=EventActions(
            state_delta={
                "missionos_adk_v2_checkpoint_ref": f"checkpoint:{task_id}",
            }
        ),
    )


def _verify_event(event: Event, *, task_id: str) -> None:
    if event.id != EVENT_ID:
        raise RuntimeError("redis_persistence_event_id_mismatch")
    if event.node_info.path != NODE_PATH:
        raise RuntimeError("redis_persistence_node_path_mismatch")
    if event.node_info.output_for != ["finalize_shadow_proposal"]:
        raise RuntimeError("redis_persistence_node_output_for_mismatch")
    output = event.output if isinstance(event.output, dict) else {}
    expected_false_fields = (
        "approval_created",
        "dispatch_authority_created",
        "executor_invoked",
        "physical_execution_invoked",
        "progress_counted",
    )
    if output.get("task_id") != task_id:
        raise RuntimeError("redis_persistence_task_id_mismatch")
    if any(output.get(field) is not False for field in expected_false_fields):
        raise RuntimeError("redis_persistence_authority_floor_mismatch")
    metadata = (
        event.custom_metadata.get("missionos_correlation", {})
        if isinstance(event.custom_metadata, dict)
        else {}
    )
    if metadata.get("task_id") != task_id:
        raise RuntimeError("redis_persistence_correlation_mismatch")


async def _write(*, session_id: str, task_id: str) -> dict[str, Any]:
    service, descriptor = _service()
    try:
        session = await service.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
            state={"missionos_task_id": task_id},
        )
        await service.append_event(session=session, event=_event(task_id=task_id))
        stored = await service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )
        if stored is None or len(stored.events) != 1:
            raise RuntimeError("redis_persistence_write_roundtrip_failed")
        _verify_event(stored.events[0], task_id=task_id)
        return {
            "mode": "write",
            "pid": os.getpid(),
            "session_id": session_id,
            "task_id": task_id,
            "event_count": len(stored.events),
            "backend": descriptor["backend"],
            "namespace": descriptor["namespace"],
        }
    finally:
        await _close_service(service)


async def _read(*, session_id: str, task_id: str) -> dict[str, Any]:
    service, descriptor = _service()
    try:
        restored = await service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )
        if restored is None:
            raise RuntimeError("redis_persistence_restored_session_missing")
        if restored.state.get("missionos_task_id") != task_id:
            raise RuntimeError("redis_persistence_session_state_mismatch")
        if restored.state.get("missionos_adk_v2_checkpoint_ref") != (
            f"checkpoint:{task_id}"
        ):
            raise RuntimeError("redis_persistence_state_delta_missing")
        if len(restored.events) != 1:
            raise RuntimeError("redis_persistence_event_count_mismatch")
        _verify_event(restored.events[0], task_id=task_id)
        return {
            "mode": "read",
            "pid": os.getpid(),
            "session_id": session_id,
            "task_id": task_id,
            "event_count": len(restored.events),
            "node_path": restored.events[0].node_info.path,
            "node_output_restored": True,
            "custom_metadata_restored": True,
            "state_delta_restored": True,
            "backend": descriptor["backend"],
            "namespace": descriptor["namespace"],
            "approval_created": False,
            "dispatch_authority_created": False,
            "executor_invoked": False,
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
    finally:
        await _close_service(service)


async def _cleanup(*, session_id: str) -> dict[str, Any]:
    service, descriptor = _service()
    try:
        await service.delete_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )
        return {
            "mode": "cleanup",
            "session_id": session_id,
            "backend": descriptor["backend"],
        }
    finally:
        await _close_service(service)


def _child(mode: str, *, session_id: str, task_id: str) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode",
        mode,
        "--session-id",
        session_id,
        "--task-id",
        task_id,
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    return json.loads(completed.stdout)


def _roundtrip(*, session_id: str, task_id: str) -> dict[str, Any]:
    _service_instance, descriptor = _service()
    asyncio.run(_close_service(_service_instance))
    writer = _child("write", session_id=session_id, task_id=task_id)
    reader = _child("read", session_id=session_id, task_id=task_id)
    cleanup = _child("cleanup", session_id=session_id, task_id=task_id)
    if writer["pid"] == reader["pid"]:
        raise RuntimeError("redis_persistence_process_restart_not_proven")
    return {
        "schema_version": "missionos_adk_v2_redis_restart_verification.v1",
        "verification_status": "passed",
        "backend": descriptor["backend"],
        "namespace": descriptor["namespace"],
        "session_id": session_id,
        "task_id": task_id,
        "writer_pid": writer["pid"],
        "reader_pid": reader["pid"],
        "separate_processes": True,
        "node_path": reader["node_path"],
        "node_output_restored": reader["node_output_restored"],
        "custom_metadata_restored": reader["custom_metadata_restored"],
        "state_delta_restored": reader["state_delta_restored"],
        "cleanup_completed": cleanup["mode"] == "cleanup",
        "approval_created": False,
        "dispatch_authority_created": False,
        "executor_invoked": False,
        "physical_execution_invoked": False,
        "progress_counted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("roundtrip", "write", "read", "cleanup"),
        default="roundtrip",
    )
    parser.add_argument("--session-id", default="")
    parser.add_argument("--task-id", default="")
    args = parser.parse_args()
    session_id = args.session_id.strip() or f"session_{uuid.uuid4().hex[:16]}"
    task_id = args.task_id.strip() or f"task_{uuid.uuid4().hex[:16]}"

    if args.mode == "roundtrip":
        result = _roundtrip(session_id=session_id, task_id=task_id)
    elif args.mode == "write":
        result = asyncio.run(_write(session_id=session_id, task_id=task_id))
    elif args.mode == "read":
        result = asyncio.run(_read(session_id=session_id, task_id=task_id))
    else:
        result = asyncio.run(_cleanup(session_id=session_id))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
