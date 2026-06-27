"""Gateway orchestration for the real-hardware arm/disarm dispatch path.

This is the single Gateway path that carries one bench arm/disarm request
through the four MissionOS boundaries and links them on ONE TaskStore task:

    Agent proposed   -> the ADK/command-override planner emits a guarded
                         ``missionos_llm_response_proposal.v1`` (proposal only).
    Human approved   -> the operator's explicit approval + deterministic gate
                         consume a single-use dispatch token bound to
                         ``px4_real_hardware`` via ``DispatchAuthorityTable``.
    Executor sent    -> ``invoke_missionos_real_hardware_dispatch_runtime`` is
                         the only code that calls the actuator backend, and it
                         fails closed unless the token is valid and the proposal
                         is the whitelisted real-hardware response.
    Verifier readback-> arm/disarm ACK + state readback are recorded by the
                         executor's canonical runtime evidence.

The orchestration never self-approves: the operator decision and the physical
attestation are inputs collected by the Gateway. With the bench gate off and no
injected fake connection the executor stays inert and touches no hardware.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
import uuid

from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.intelligence.real_hardware_arm_disarm_planner import (
    run_real_hardware_arm_disarm_planner,
)
from src.runtime.missionos_real_hardware_dispatch_runtime import (
    REAL_HARDWARE_DISPATCH_BACKEND_TARGET,
    invoke_missionos_real_hardware_dispatch_runtime,
)
from src.runtime.px4_real_hardware_actuator_backend import (
    PX4RealHardwareActuatorApproval,
)


REAL_HARDWARE_DISPATCH_ORCHESTRATION_SCHEMA_VERSION = (
    "missionos_real_hardware_dispatch_orchestration.v1"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attach(store: Any, task_id: str, key: str, value: Mapping[str, Any]) -> None:
    current = store.get(task_id)
    if current is None:
        raise ValueError(f"task not found while attaching dispatch stage: {task_id}")
    existing = list(current.get("artifacts", {}).get(key, []))
    store.update(task_id, artifacts={key: [*existing, dict(value)]})


def run_real_hardware_arm_disarm_dispatch(
    *,
    store: Any,
    task_id: str,
    subject_id: str,
    artifact_root: Path | str,
    artifact_relative: Callable[[Path], str],
    authority_table_state_path: Path | str,
    actuator_approval: PX4RealHardwareActuatorApproval,
    operator_approved: bool,
    bench_context: Mapping[str, Any] | None = None,
    operator_instruction: Mapping[str, Any] | None = None,
    serial_device: str | None = None,
    baudrate: int = 57600,
    opt_in: bool = False,
    connection_factory: Callable[[], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Run the planner -> authority -> approval -> executor chain on one task."""

    generated_at = _utc_now()
    session_id = f"missionos_real_hardware_dispatch_session_{uuid.uuid4().hex[:12]}"

    # Stage 1 (Agent proposed): the planner only proposes.
    planner_result = run_real_hardware_arm_disarm_planner(
        artifact_root=artifact_root,
        artifact_relative=artifact_relative,
        bench_context=bench_context,
        operator_instruction=operator_instruction,
    )
    if planner_result.get("planner_status") != "proposal_guardrail_passed":
        _attach(
            store,
            task_id,
            "missionos_real_hardware_dispatch_orchestration",
            {
                "schema_version": REAL_HARDWARE_DISPATCH_ORCHESTRATION_SCHEMA_VERSION,
                "session_id": session_id,
                "generated_at": generated_at,
                "orchestration_status": "blocked_at_agent_proposal",
                "planner_status": planner_result.get("planner_status"),
                "planner_blocking_reasons": planner_result.get("blocking_reasons"),
            },
        )
        return {
            "orchestration_status": "blocked_at_agent_proposal",
            "session_id": session_id,
            "planner_result": planner_result,
            "runtime_invoked": False,
        }
    proposal = planner_result["proposal"]

    # Stage 2 (Human approved): register a real-hardware authority and consume a
    # single-use token only when the operator actually approved.
    authority_id = f"real_hardware_arm_disarm_authority_{uuid.uuid4().hex[:12]}"
    dispatch_ref = f"real_hardware_arm_disarm_dispatch:{session_id}"
    table = DispatchAuthorityTable(Path(authority_table_state_path))
    table.register_authority(
        {
            "dispatch_authority_id": authority_id,
            "dispatch_ref": dispatch_ref,
            "operator_approval_required": True,
            "automatic_dispatch_suppressed": True,
            "bounded_action_ref": proposal.get("proposal_ref")
            or planner_result.get("proposal_ref"),
        },
        artifact_path=planner_result.get("proposal_artifact_path", ""),
        backend_target=REAL_HARDWARE_DISPATCH_BACKEND_TARGET,
    )

    approval_id = f"operator_dispatch_approval_{uuid.uuid4().hex[:12]}"
    gate_result_id = f"deterministic_dispatch_gate_{uuid.uuid4().hex[:12]}"
    operator_approval_record = {
        "approval_id": approval_id,
        "session_id": session_id,
        "operator_approved": bool(operator_approved),
        "automatic_dispatch_executed": False,
    }
    deterministic_gate_record = {
        "gate_result_id": gate_result_id,
        "session_id": session_id,
        "deterministic_gate_passed": bool(operator_approved),
        "automatic_dispatch_executed": False,
    }
    dispatch_validation = table.validate_dispatch_request(
        authority_id=authority_id,
        operator_approval=operator_approval_record,
        deterministic_gate=deterministic_gate_record,
    )

    # Stage 3/4 (Executor sent / Verifier readback): the executor is the only
    # path to the backend; it fails closed unless the token is valid.
    runtime_result = invoke_missionos_real_hardware_dispatch_runtime(
        store=store,
        task_id=task_id,
        subject_id=subject_id,
        approval=actuator_approval,
        dispatch_validation=dispatch_validation,
        llm_response_proposal=proposal,
        serial_device=serial_device,
        baudrate=baudrate,
        opt_in=opt_in,
        connection_factory=connection_factory,
        clock=clock,
    )

    orchestration_status = (
        "executed" if runtime_result.get("runtime_invoked") is True else "blocked_at_executor"
    )
    orchestration = {
        "schema_version": REAL_HARDWARE_DISPATCH_ORCHESTRATION_SCHEMA_VERSION,
        "session_id": session_id,
        "generated_at": generated_at,
        "orchestration_status": orchestration_status,
        "backend_target": REAL_HARDWARE_DISPATCH_BACKEND_TARGET,
        "agent_proposed": {
            "proposal_ref": planner_result.get("proposal_ref"),
            "proposal_artifact_path": planner_result.get("proposal_artifact_path"),
            "response_kind": proposal.get("response_kind"),
        },
        "human_approved": {
            "authority_id": authority_id,
            "approval_id": approval_id,
            "gate_result_id": gate_result_id,
            "operator_approved": bool(operator_approved),
            "validation_status": dispatch_validation.get("validation_status"),
            "backend_target": dispatch_validation.get("backend_target"),
            "dispatch_replay_detected": dispatch_validation.get(
                "dispatch_replay_detected"
            )
            is True,
        },
        "executor_sent": {
            "runtime_invoked": runtime_result.get("runtime_invoked") is True,
            "blocked_reason": runtime_result.get("blocked_reason"),
        },
    }
    _attach(
        store,
        task_id,
        "missionos_real_hardware_dispatch_orchestration",
        orchestration,
    )

    return {
        "orchestration_status": orchestration_status,
        "session_id": session_id,
        "planner_result": planner_result,
        "dispatch_validation": dispatch_validation,
        "runtime_result": runtime_result,
        "runtime_invoked": runtime_result.get("runtime_invoked") is True,
    }


__all__ = [
    "REAL_HARDWARE_DISPATCH_ORCHESTRATION_SCHEMA_VERSION",
    "run_real_hardware_arm_disarm_dispatch",
]
