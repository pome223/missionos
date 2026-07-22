"""Production Gateway routes for the backend-neutral hardware adapter runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import uuid

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.gateway.missionos_milestone import ARTIFACT_ROOT
from src.runtime.hardware_adapter_contract import HardwareOperatorApproval
from src.runtime.hardware_adapter_registrations import (
    build_default_hardware_adapter_registry,
)
from src.runtime.hardware_adapter_runtime import (
    HARDWARE_ADAPTER_APPROVAL_TTL,
    HardwareAdapterPreparation,
    HardwareAdapterRegistry,
    HardwareAdapterRegistryError,
    HardwareAdapterRuntimeError,
    HardwareAdapterRuntimeRequest,
    dispatch_hardware_adapter_action,
    prepare_hardware_adapter_action,
)


def _preparation_collection(task: dict[str, Any]) -> dict[str, Any]:
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    collection = artifacts.get("missionos_hardware_adapter_preparations")
    return collection if isinstance(collection, dict) else {}


def build_hardware_adapter_router(
    *,
    task_store: Any,
    resolve_http_user_id: Callable[..., str],
    registry: HardwareAdapterRegistry | None = None,
) -> APIRouter:
    """Build generic prepare and approved-dispatch routes."""

    router = APIRouter()
    resolved_registry = registry or build_default_hardware_adapter_registry()

    def finalize_claimed_preparation(
        *,
        task_id: str,
        preparation_ref: str,
        approval_ref: str,
        lifecycle_status: str,
        artifacts: dict[str, Any],
    ) -> dict[str, Any] | None:
        return task_store.claim_nested_artifact(
            task_id,
            collection_key="missionos_hardware_adapter_preparations",
            artifact_id=preparation_ref,
            expected={
                "lifecycle_status": "dispatching",
                "claimed_by_approval_ref": approval_ref,
            },
            updates={"lifecycle_status": lifecycle_status},
            artifacts=artifacts,
        )

    @router.post("/missionos/hardware-adapters/prepare")
    async def prepare_hardware_adapter(
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        body = payload or {}
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            raise HTTPException(status_code=400, detail="task_id is required")
        task = task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        try:
            request_model = HardwareAdapterRuntimeRequest.model_validate(
                body.get("request")
            )
            preparation = prepare_hardware_adapter_action(
                registry=resolved_registry,
                request=request_model,
            )
        except (
            HardwareAdapterRegistryError,
            HardwareAdapterRuntimeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        stored = {
            **preparation.model_dump(mode="json"),
            "lifecycle_status": "prepared",
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        existing = _preparation_collection(task).get(preparation.preparation_ref)
        if isinstance(existing, dict):
            if existing.get("preparation_sha256") != preparation.preparation_sha256:
                raise HTTPException(
                    status_code=409,
                    detail="preparation ref already exists with another hash",
                )
            return {
                "task_id": task_id,
                "preparation": existing,
                "registered_adapter_ids": resolved_registry.registered_adapter_ids(),
                "dispatch_request_sent": False,
                "operator_approval_required": True,
                "existing_preparation_reused": True,
            }
        task_store.update(
            task_id,
            artifacts={
                "missionos_hardware_adapter_preparations": {
                    preparation.preparation_ref: stored,
                }
            },
        )
        return {
            "task_id": task_id,
            "preparation": stored,
            "registered_adapter_ids": resolved_registry.registered_adapter_ids(),
            "dispatch_request_sent": False,
            "operator_approval_required": True,
            "existing_preparation_reused": False,
        }

    @router.post("/missionos/hardware-adapters/approve-and-dispatch")
    async def approve_and_dispatch_hardware_adapter(
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        body = payload or {}
        task_id = str(body.get("task_id") or "").strip()
        preparation_ref = str(body.get("preparation_ref") or "").strip()
        preparation_sha256 = str(body.get("preparation_sha256") or "").strip()
        if not task_id or not preparation_ref or not preparation_sha256:
            raise HTTPException(
                status_code=400,
                detail="task_id, preparation_ref, and preparation_sha256 are required",
            )
        task = task_store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        stored = _preparation_collection(task).get(preparation_ref)
        if not isinstance(stored, dict):
            raise HTTPException(status_code=404, detail="preparation not found")
        if body.get("operator_approved") is not True:
            raise HTTPException(status_code=409, detail="operator approval required")

        try:
            preparation = HardwareAdapterPreparation.model_validate(
                {
                    key: stored.get(key)
                    for key in HardwareAdapterPreparation.model_fields
                }
            )
        except ValidationError as exc:
            raise HTTPException(status_code=409, detail="preparation invalid") from exc
        if preparation.preparation_sha256 != preparation_sha256:
            raise HTTPException(status_code=409, detail="preparation hash mismatch")

        request_model = HardwareAdapterRuntimeRequest.model_validate(preparation.request)
        approval_actor = resolve_http_user_id(
            request,
            None,
            default_user_id="loopback_local_operator",
        )
        approval_ref = f"hardware_adapter_approval:{uuid.uuid4().hex[:16]}"
        approved_at = datetime.now(timezone.utc)
        approval = HardwareOperatorApproval(
            operator_approval_ref=approval_ref,
            approved_adapter_id=request_model.adapter_id,
            approved_preparation_ref=preparation.preparation_ref,
            approved_preparation_sha256=preparation.preparation_sha256,
            approval_actor=approval_actor,
            approval_timestamp=approved_at,
            approval_expires_at=approved_at + HARDWARE_ADAPTER_APPROVAL_TTL,
            approved_action_ref=request_model.missionos_action_ref,
            approved_action_kind=request_model.action_kind,
        )

        claimed = task_store.claim_nested_artifact(
            task_id,
            collection_key="missionos_hardware_adapter_preparations",
            artifact_id=preparation_ref,
            expected={
                "lifecycle_status": "prepared",
                "preparation_sha256": preparation_sha256,
            },
            updates={
                "lifecycle_status": "dispatching",
                "claimed_by_approval_ref": approval_ref,
            },
            artifacts={
                "missionos_hardware_adapter_approvals": {
                    approval_ref: approval.model_dump(mode="json"),
                }
            },
        )
        if claimed is None:
            raise HTTPException(
                status_code=409,
                detail="preparation is stale, already claimed, or superseded",
            )

        try:
            authority_id = f"hardware_adapter_authority:{uuid.uuid4().hex[:16]}"
            gate_result_id = f"hardware_adapter_gate:{uuid.uuid4().hex[:16]}"
            table = DispatchAuthorityTable(
                Path(ARTIFACT_ROOT)
                / "missionos_hardware_adapter_authority"
                / "authority_table_state.json"
            )
            table.register_authority(
                {
                    "dispatch_authority_id": authority_id,
                    "dispatch_ref": f"hardware_adapter_dispatch:{preparation_ref}",
                    "bounded_action_ref": request_model.missionos_action_ref,
                    "approval_ref": approval_ref,
                    "operator_approval_required": True,
                    "automatic_dispatch_suppressed": True,
                },
                artifact_path=preparation_ref,
                backend_target=f"hardware_adapter:{request_model.adapter_id}",
            )
            validation = table.validate_dispatch_request(
                authority_id=authority_id,
                operator_approval={
                    "approval_id": approval_ref,
                    "operator_approved": True,
                    "automatic_dispatch_executed": False,
                },
                deterministic_gate={
                    "gate_result_id": gate_result_id,
                    "deterministic_gate_passed": True,
                    "automatic_dispatch_executed": False,
                },
            )
            if validation.get("validation_status") != "valid":
                raise HardwareAdapterRuntimeError("dispatch_authority_invalid")
            result = await run_in_threadpool(
                dispatch_hardware_adapter_action,
                registry=resolved_registry,
                preparation=preparation,
                approval=approval,
            )
        except (
            HardwareAdapterRegistryError,
            HardwareAdapterRuntimeError,
            ValidationError,
            ValueError,
            RuntimeError,
        ) as exc:
            error_payload = {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "dispatch_status": "blocked",
                "dispatch_request_sent": False,
                "physical_execution_invoked": False,
                "completion_claimed": False,
            }
            failed = finalize_claimed_preparation(
                task_id=task_id,
                preparation_ref=preparation_ref,
                approval_ref=approval_ref,
                lifecycle_status="failed",
                artifacts={
                    "missionos_hardware_adapter_runtime_errors": {
                        preparation_ref: error_payload,
                    }
                },
            )
            if failed is None:
                task_store.update(
                    task_id,
                    artifacts={
                        "missionos_hardware_adapter_reconciliation_errors": {
                            preparation_ref: {
                                "reason": "failed_transition_cas_rejected",
                                **error_payload,
                            }
                        }
                    },
                )
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        result_payload = result.model_dump(mode="json")
        finalized = finalize_claimed_preparation(
            task_id=task_id,
            preparation_ref=preparation_ref,
            approval_ref=approval_ref,
            lifecycle_status=(
                "verified" if result.completion_claimed else "blocked_or_unverified"
            ),
            artifacts={
                "missionos_hardware_adapter_dispatch_validations": {
                    authority_id: validation,
                },
                "missionos_hardware_adapter_runtime_results": {
                    preparation_ref: result_payload,
                },
            },
        )
        if finalized is None:
            task_store.update(
                task_id,
                artifacts={
                    "missionos_hardware_adapter_reconciliation_errors": {
                        preparation_ref: {
                            "reason": "terminal_transition_cas_rejected",
                            "dispatch_status": result.dispatch_status,
                            "dispatch_request_sent": result.dispatch_request_sent,
                            "runtime_result": result_payload,
                            "dispatch_validation": validation,
                            "physical_execution_invoked": (
                                result.physical_execution_invoked
                            ),
                            "completion_claimed": result.completion_claimed,
                        }
                    }
                },
            )
            raise HTTPException(
                status_code=409,
                detail="runtime completed but lifecycle finalization failed",
            )
        return {
            "task_id": task_id,
            "dispatch_validation": validation,
            "runtime_result": result_payload,
        }

    return router


__all__ = ["build_hardware_adapter_router"]
