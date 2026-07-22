"""HTTP routes for the bounded PX4 real-hardware bench boundary.

This module owns transport validation and dependency wiring only. It does not
mint approval, decide a mission action, send MAVLink, or verify completion. The
existing orchestration, executor, and verifier modules retain those separate
responsibilities.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.gateway.missionos_milestone import ARTIFACT_ROOT, _relative
from src.gateway.missionos_real_hardware_dispatch import (
    run_real_hardware_arm_disarm_dispatch,
)
from src.runtime.px4_real_hardware_actuator_backend import (
    PX4RealHardwareActuatorError,
    build_px4_real_hardware_actuator_approval,
)


def build_real_hardware_router(
    *,
    task_store: Any,
    resolve_http_user_id: Callable[..., str],
) -> APIRouter:
    """Return the Gateway router for approval-gated PX4 bench dispatch."""

    router = APIRouter()

    @router.post("/missionos/real-hardware-arm-disarm-dispatch/run")
    async def missionos_real_hardware_arm_disarm_dispatch_run(
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        body = payload or {}
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            raise HTTPException(status_code=400, detail="task_id is required")
        if task_store.get(task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")
        subject_id = str(body.get("subject_id") or "").strip()
        if not subject_id:
            raise HTTPException(status_code=400, detail="subject_id is required")
        attestation = body.get("physical_attestation")
        if not isinstance(attestation, dict):
            raise HTTPException(
                status_code=400,
                detail="physical_attestation object is required",
            )

        # Attestation and approval are Gateway-collected inputs. The executor
        # still requires its independent real-serial opt-in before any send.
        try:
            actuator_approval = build_px4_real_hardware_actuator_approval(
                approved_operations=("arm", "disarm"),
                physical_attestation=attestation,
            )
        except (PX4RealHardwareActuatorError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        approval_actor = resolve_http_user_id(
            request,
            None,
            default_user_id="loopback_local_operator",
        )

        def _run() -> dict[str, Any]:
            return run_real_hardware_arm_disarm_dispatch(
                store=task_store,
                task_id=task_id,
                subject_id=subject_id,
                artifact_root=ARTIFACT_ROOT,
                artifact_relative=_relative,
                authority_table_state_path=(
                    Path(ARTIFACT_ROOT)
                    / "missionos_real_hardware_dispatch_authority"
                    / "authority_table_state.json"
                ),
                actuator_approval=actuator_approval,
                operator_approved=body.get("operator_approved") is True,
                approval_actor=approval_actor,
                bench_context=(
                    body.get("bench_context")
                    if isinstance(body.get("bench_context"), dict)
                    else None
                ),
                operator_instruction=(
                    body.get("operator_instruction")
                    if isinstance(body.get("operator_instruction"), dict)
                    else None
                ),
                serial_device=(
                    str(body["serial_device"]) if body.get("serial_device") else None
                ),
                opt_in=body.get("opt_in") is True,
            )

        return await run_in_threadpool(_run)

    return router


__all__ = ["build_real_hardware_router"]
