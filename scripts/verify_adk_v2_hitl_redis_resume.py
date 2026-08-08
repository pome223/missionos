#!/usr/bin/env python3
"""Verify ADK v2 RequestInput pause/resume across Redis client processes."""

from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Mapping
import uuid

from src.intelligence.missionos_adk_v2_hitl import (
    MISSIONOS_ADK_V2_HITL_APP_NAME,
    resume_missionos_canonical_approval_hitl,
    start_missionos_canonical_approval_hitl,
)
from src.runtime.session_service import (
    RedisSessionService,
    create_session_service,
    describe_session_backend,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        redis_url=os.environ.get("REDIS_URL", "").strip(),
        redis_session_namespace=os.environ.get(
            "REDIS_SESSION_NAMESPACE",
            "missionos:adk-v2:hitl-smoke",
        ).strip(),
    )


def _service() -> tuple[RedisSessionService, dict[str, Any]]:
    settings = _settings()
    descriptor = describe_session_backend(settings)
    if not settings.redis_url:
        raise RuntimeError("redis_hitl_smoke_requires_REDIS_URL")
    if descriptor.get("backend") != "redis":
        raise RuntimeError("redis_hitl_smoke_backend_not_redis")
    service = create_session_service(settings, require_redis=True)
    if not isinstance(service, RedisSessionService):
        raise RuntimeError("redis_hitl_smoke_service_not_redis")
    return service, descriptor


async def _close_service(service: RedisSessionService) -> None:
    close = getattr(service._client, "aclose", None)
    if callable(close):
        await close()


def _binding(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "binding_status": "ready",
        "blocking_reasons": [],
        "approval_ref": f"missionos_fixture_approval:{task_id}",
        "mission_response_candidate_ref": f"mission_response_candidate:{task_id}",
        "proposal_sha256": sha256(task_id.encode("utf-8")).hexdigest(),
        "bounded_action_ref": f"bounded_action:{task_id}",
        "dispatch_ref": f"dispatch:{task_id}",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


def _canonical_fixture_validator(task_id: str):
    expected_fixture = _binding(task_id)

    def validate(
        approval_ref: str,
        expected_binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        reasons: list[str] = []
        if approval_ref != expected_fixture["approval_ref"]:
            reasons.append("fixture_canonical_approval_ref_mismatch")
        for field in (
            "mission_response_candidate_ref",
            "proposal_sha256",
            "bounded_action_ref",
            "dispatch_ref",
        ):
            if expected_binding.get(field) != expected_fixture[field]:
                reasons.append(f"fixture_canonical_binding_mismatch:{field}")
        return {
            "validation_status": "approved" if not reasons else "blocked",
            "blocking_reasons": reasons,
            "approval_ref": approval_ref,
            "canonical_approval_validated": not reasons,
            "approval_created": False,
            "approval_consumed": False,
            "dispatch_authority_created": False,
            "executor_invoked": False,
            "physical_execution_invoked": False,
            "outcome_observed": False,
            "progress_counted": False,
        }

    return validate


async def _pause(
    *,
    adk_session_id: str,
    operator_session_id: str,
    task_id: str,
) -> dict[str, Any]:
    service, descriptor = _service()
    try:
        result = await start_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id=operator_session_id,
            approval_binding=_binding(task_id),
            approval_validator=_canonical_fixture_validator(task_id),
            adk_session_id=adk_session_id,
        )
        if result.get("hitl_status") != "awaiting_canonical_approval":
            raise RuntimeError("redis_hitl_smoke_pause_failed")
        return {
            "mode": "pause",
            "pid": os.getpid(),
            "backend": descriptor["backend"],
            **result,
        }
    finally:
        await _close_service(service)


async def _resume(
    *,
    adk_session_id: str,
    operator_session_id: str,
    task_id: str,
) -> dict[str, Any]:
    service, descriptor = _service()
    try:
        result = await resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id=operator_session_id,
            adk_session_id=adk_session_id,
            human_response={"approval_ref": _binding(task_id)["approval_ref"]},
            approval_validator=_canonical_fixture_validator(task_id),
        )
        if result.get("hitl_status") != "canonical_approval_validated":
            raise RuntimeError("redis_hitl_smoke_resume_failed")
        return {
            "mode": "resume",
            "pid": os.getpid(),
            "backend": descriptor["backend"],
            **result,
        }
    finally:
        await _close_service(service)


async def _yes(
    *,
    adk_session_id: str,
    operator_session_id: str,
    task_id: str,
) -> dict[str, Any]:
    service, descriptor = _service()
    try:
        result = await resume_missionos_canonical_approval_hitl(
            session_service=service,
            operator_session_id=operator_session_id,
            adk_session_id=adk_session_id,
            human_response={"response": "yes"},
            approval_validator=_canonical_fixture_validator(task_id),
        )
        if result.get("resume_attempted") is not False:
            raise RuntimeError("redis_hitl_smoke_yes_resumed_workflow")
        return {
            "mode": "yes",
            "pid": os.getpid(),
            "backend": descriptor["backend"],
            **result,
        }
    finally:
        await _close_service(service)


async def _cleanup(
    *,
    adk_session_id: str,
    operator_session_id: str,
) -> dict[str, Any]:
    service, descriptor = _service()
    try:
        await service.delete_session(
            app_name=MISSIONOS_ADK_V2_HITL_APP_NAME,
            user_id=operator_session_id,
            session_id=adk_session_id,
        )
        return {
            "mode": "cleanup",
            "pid": os.getpid(),
            "backend": descriptor["backend"],
            "adk_session_id": adk_session_id,
        }
    finally:
        await _close_service(service)


def _child(
    mode: str,
    *,
    adk_session_id: str,
    operator_session_id: str,
    task_id: str,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode",
        mode,
        "--adk-session-id",
        adk_session_id,
        "--operator-session-id",
        operator_session_id,
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


def _roundtrip(
    *,
    adk_session_id: str,
    operator_session_id: str,
    task_id: str,
) -> dict[str, Any]:
    service, descriptor = _service()
    asyncio.run(_close_service(service))
    yes_session_id = f"{adk_session_id}_yes"
    yes_operator_session_id = f"{operator_session_id}_yes"
    paused = _child(
        "pause",
        adk_session_id=adk_session_id,
        operator_session_id=operator_session_id,
        task_id=task_id,
    )
    resumed = _child(
        "resume",
        adk_session_id=adk_session_id,
        operator_session_id=operator_session_id,
        task_id=task_id,
    )
    yes_paused = _child(
        "pause",
        adk_session_id=yes_session_id,
        operator_session_id=yes_operator_session_id,
        task_id=task_id,
    )
    yes_result = _child(
        "yes",
        adk_session_id=yes_session_id,
        operator_session_id=yes_operator_session_id,
        task_id=task_id,
    )
    cleanup = _child(
        "cleanup",
        adk_session_id=adk_session_id,
        operator_session_id=operator_session_id,
        task_id=task_id,
    )
    yes_cleanup = _child(
        "cleanup",
        adk_session_id=yes_session_id,
        operator_session_id=yes_operator_session_id,
        task_id=task_id,
    )
    if paused["pid"] == resumed["pid"]:
        raise RuntimeError("redis_hitl_smoke_process_restart_not_proven")
    if resumed.get("canonical_approval_validated") is not True:
        raise RuntimeError("redis_hitl_smoke_canonical_approval_not_validated")
    if yes_result.get("canonical_approval_validated") is not False:
        raise RuntimeError("redis_hitl_smoke_yes_created_approval")
    return {
        "schema_version": "missionos_adk_v2_hitl_redis_resume_verification.v1",
        "verification_status": "passed",
        "backend": descriptor["backend"],
        "namespace": descriptor["namespace"],
        "adk_session_id": adk_session_id,
        "operator_session_id": operator_session_id,
        "task_id": task_id,
        "pause_pid": paused["pid"],
        "resume_pid": resumed["pid"],
        "separate_processes": True,
        "pause_status": paused["hitl_status"],
        "resume_status": resumed["hitl_status"],
        "checkpoint_restored": resumed["checkpoint_restored"],
        "canonical_approval_validated": resumed[
            "canonical_approval_validated"
        ],
        "yes_pause_status": yes_paused["hitl_status"],
        "yes_status": yes_result["hitl_status"],
        "yes_resume_attempted": yes_result["resume_attempted"],
        "yes_created_approval": False,
        "cleanup_completed": (
            cleanup["mode"] == "cleanup" and yes_cleanup["mode"] == "cleanup"
        ),
        "approval_created": False,
        "dispatch_authority_created": False,
        "executor_invoked": False,
        "physical_execution_invoked": False,
        "outcome_observed": False,
        "progress_counted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("roundtrip", "pause", "resume", "yes", "cleanup"),
        default="roundtrip",
    )
    parser.add_argument("--adk-session-id", default="")
    parser.add_argument("--operator-session-id", default="")
    parser.add_argument("--task-id", default="")
    args = parser.parse_args()
    adk_session_id = args.adk_session_id.strip() or f"hitl_{uuid.uuid4().hex[:16]}"
    operator_session_id = (
        args.operator_session_id.strip() or f"operator_{uuid.uuid4().hex[:16]}"
    )
    task_id = args.task_id.strip() or f"task_{uuid.uuid4().hex[:16]}"
    kwargs = {
        "adk_session_id": adk_session_id,
        "operator_session_id": operator_session_id,
        "task_id": task_id,
    }
    if args.mode == "roundtrip":
        result = _roundtrip(**kwargs)
    elif args.mode == "pause":
        result = asyncio.run(_pause(**kwargs))
    elif args.mode == "resume":
        result = asyncio.run(_resume(**kwargs))
    elif args.mode == "yes":
        result = asyncio.run(_yes(**kwargs))
    else:
        result = asyncio.run(
            _cleanup(
                adk_session_id=adk_session_id,
                operator_session_id=operator_session_id,
            )
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
