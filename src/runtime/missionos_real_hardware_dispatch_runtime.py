"""Agent -> Gateway -> Executor wiring for the real-hardware actuator path.

This module is the deterministic executor that sits between an operator-approved
Gateway dispatch authorization and the real-hardware actuator backend. It does
NOT decide anything: the ADK Agent only proposes (a whitelisted, proposal-only
``operator_gated_real_hardware_arm_disarm`` response), the Gateway consumes the
single-use dispatch token (``DispatchAuthorityTable.validate_dispatch_request``),
and only then does this runtime call the backend and emit canonical runtime
evidence. With the bench gate off and no injected fake connection it is inert and
touches no hardware.

The evidence this runtime emits is the canonical ``runtime_invocation_evidence.v1``
(``invocation_kind="mavlink"``) — distinct from the backend's own
``px4_real_hardware_actuator_command_evidence.v1`` command-level record, which it
links rather than reproduces.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Any, Callable, Mapping

from src.runtime.missionos_llm_schemas import (
    MissionOSLLMSchemaValidationError,
    validate_llm_response_proposal,
)
from src.runtime.px4_real_hardware_actuator_backend import (
    LINK_KIND_REAL_SERIAL_PYMAVLINK,
    PX4RealHardwareActuatorApproval,
    run_px4_real_hardware_arm_disarm_bench,
)
from src.runtime.runtime_claim_evidence import (
    RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION,
    validate_runtime_invocation_evidence,
)


MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME_OPT_IN_ENV = (
    "RUN_MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME"
)
REAL_HARDWARE_DISPATCH_BACKEND_TARGET = "px4_real_hardware"
REAL_HARDWARE_DISPATCH_RESPONSE_KIND = "operator_gated_real_hardware_arm_disarm"
REAL_HARDWARE_DISPATCH_STAGE_EVIDENCE_SCHEMA_VERSION = (
    "missionos_real_hardware_dispatch_stage_evidence.v1"
)
_INJECTED_FAKE_INVOCATION_TARGET = "injected_fake_connection"


class MissionOSRealHardwareDispatchError(ValueError):
    """Raised when the real-hardware dispatch wiring is misused."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(payload: Any) -> str:
    return _sha256_text(json.dumps(payload, sort_keys=True, default=str))


def _opt_in_enabled() -> bool:
    return os.getenv(MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME_OPT_IN_ENV) == "1"


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return {"runtime_invoked": False, "blocked_reason": reason, **extra}


def _persist_list(store: Any, task_id: str, key: str, value: Mapping[str, Any]) -> None:
    current = store.get(task_id)
    if current is None:
        raise MissionOSRealHardwareDispatchError(
            f"task not found while attaching dispatch evidence: {task_id}"
        )
    existing = list(current.get("artifacts", {}).get(key, []))
    store.update(task_id, artifacts={key: [*existing, dict(value)]})


def invoke_missionos_real_hardware_dispatch_runtime(
    *,
    store: Any,
    task_id: str,
    subject_id: str,
    approval: PX4RealHardwareActuatorApproval,
    dispatch_validation: Mapping[str, Any],
    llm_response_proposal: Mapping[str, Any],
    serial_device: str | None = None,
    baudrate: int = 57600,
    opt_in: bool = False,
    connection_factory: Callable[[], Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Run the operator-approved real-hardware arm/disarm dispatch.

    Fails closed unless the Gateway already produced a *valid* dispatch
    validation and the Agent proposal is the whitelisted, proposal-only
    real-hardware response. With no injected ``connection_factory`` the bench
    path additionally requires the opt-in env gate and ``opt_in=True``; otherwise
    the runtime is inert and touches no hardware.
    """

    # Stage 1/2: the Agent only proposed; the Gateway owns approval + the
    # single-use dispatch token. Neither is decided here.
    if str(dispatch_validation.get("validation_status")) != "valid":
        return _blocked(
            "dispatch_validation_not_valid",
            dispatch_validation_status=dispatch_validation.get("validation_status"),
        )
    if dispatch_validation.get("operator_approval_consumed") is not True:
        return _blocked("operator_approval_not_consumed")
    # The token must have been minted for THIS backend. Without this check a valid
    # px4_gazebo_sitl authorization could be replayed into the real-hardware
    # executor, since the rest of the validation shape is identical.
    if dispatch_validation.get("backend_target") != REAL_HARDWARE_DISPATCH_BACKEND_TARGET:
        return _blocked(
            "backend_target_not_real_hardware",
            backend_target=dispatch_validation.get("backend_target"),
        )

    try:
        proposal = validate_llm_response_proposal(llm_response_proposal)
    except MissionOSLLMSchemaValidationError as exc:
        return _blocked(f"llm_response_proposal_invalid:{exc}")
    if proposal.get("response_kind") != REAL_HARDWARE_DISPATCH_RESPONSE_KIND:
        return _blocked(
            "response_kind_not_real_hardware_dispatch",
            response_kind=proposal.get("response_kind"),
        )

    # Bench gate: a real serial run needs the opt-in env and opt_in=True. A fake
    # injected connection bypasses the gate because it never touches hardware.
    if connection_factory is None:
        if not _opt_in_enabled():
            return _blocked(
                f"{MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME_OPT_IN_ENV}_not_enabled"
            )
        if opt_in is not True or not serial_device:
            return _blocked("real_serial_dispatch_requires_opt_in_and_serial_device")

    started_at = _utc_now()
    result = run_px4_real_hardware_arm_disarm_bench(
        store=store,
        task_id=task_id,
        subject_id=subject_id,
        approval=approval,
        serial_device=serial_device,
        baudrate=baudrate,
        opt_in=opt_in,
        connection_factory=connection_factory,
        clock=clock,
    )
    completed_at = _utc_now()

    arm = result["arm"]
    disarm = result["disarm"]
    link_kind = str(arm["link_kind"])
    physical_execution_invoked = bool(arm["physical_execution_invoked"])
    invocation_target = serial_device or _INJECTED_FAKE_INVOCATION_TARGET
    result_json = json.dumps(result, sort_keys=True, default=str)

    runtime_invocation_evidence = {
        "schema_version": RUNTIME_INVOCATION_EVIDENCE_SCHEMA_VERSION,
        "invocation_kind": "mavlink",
        "invocation_target": invocation_target,
        "invocation_started_at": started_at,
        "invocation_completed_at": completed_at,
        "invocation_stdout_sha256": _sha256_text(result_json),
        "invocation_stderr_sha256": _sha256_text(""),
        "invocation_exit_code": 0,
        "backend_target": REAL_HARDWARE_DISPATCH_BACKEND_TARGET,
        "link_kind": link_kind,
        "physical_execution_invoked": physical_execution_invoked,
        "flight_execution_invoked": False,
        "opt_in_env": _opt_in_enabled(),
        "command_evidence_refs": [
            {
                "operation": arm["operation"],
                "schema_version": arm["command_evidence"]["schema_version"],
                "invocation_id": arm["invocation_id"],
            },
            {
                "operation": disarm["operation"],
                "schema_version": disarm["command_evidence"]["schema_version"],
                "invocation_id": disarm["invocation_id"],
            },
        ],
    }
    # Fail closed: this must satisfy the canonical validator, not merely borrow
    # its name (the defect the command-level record was renamed away from).
    validate_runtime_invocation_evidence(runtime_invocation_evidence)

    invocation_evidence = proposal.get("llm_invocation_evidence", {})
    stage_evidence = {
        "schema_version": REAL_HARDWARE_DISPATCH_STAGE_EVIDENCE_SCHEMA_VERSION,
        "observed_at": completed_at,
        "backend_target": REAL_HARDWARE_DISPATCH_BACKEND_TARGET,
        "subject_id": subject_id,
        "agent_proposed": {
            "response_kind": proposal.get("response_kind"),
            "operator_approved": proposal.get("operator_approved"),
            "dispatch_authority_created": proposal.get("dispatch_authority_created"),
            "drone_physics_affected": proposal.get("drone_physics_affected"),
            "llm_invocation_evidence_sha256": _sha256_json(invocation_evidence),
            "llm_response_proposal_sha256": _sha256_json(dict(proposal)),
        },
        "human_approved": {
            "authority_id": dispatch_validation.get("authority_id"),
            "approval_id": dispatch_validation.get("operator_approval_id"),
            "gate_result_id": dispatch_validation.get("gate_result_id"),
            "operator_approval_consumed": dispatch_validation.get(
                "operator_approval_consumed"
            )
            is True,
            "dispatch_replay_detected": dispatch_validation.get(
                "dispatch_replay_detected"
            )
            is True,
        },
        "executor_sent": {
            "arm_mavlink_command_sent": arm["mavlink_command_sent"],
            "disarm_mavlink_command_sent": disarm["mavlink_command_sent"],
            "link_kind": link_kind,
            "physical_execution_invoked": physical_execution_invoked,
        },
        "verifier_readback_observed": {
            "arm_status": arm["status"],
            "disarm_status": disarm["status"],
            "arm_state_readback_observed": arm["state_readback_observed"],
            "disarm_state_readback_observed": disarm["state_readback_observed"],
        },
    }

    _persist_list(
        store,
        task_id,
        "missionos_real_hardware_dispatch_runtime_invocations",
        runtime_invocation_evidence,
    )
    _persist_list(
        store,
        task_id,
        "missionos_real_hardware_dispatch_stage_evidence",
        stage_evidence,
    )

    return {
        "runtime_invoked": True,
        "backend_target": REAL_HARDWARE_DISPATCH_BACKEND_TARGET,
        "response_kind": proposal.get("response_kind"),
        "runtime_invocation_evidence": runtime_invocation_evidence,
        "dispatch_stage_evidence": stage_evidence,
        "arm": arm,
        "disarm": disarm,
    }


def real_hardware_dispatch_runtime_summary_supports_dispatch(
    summary: Mapping[str, Any],
) -> bool:
    """True only when evidence proves a real, physical, bounded bench dispatch.

    A fake/injected run is honestly *not* supported here: it leaves
    ``physical_execution_invoked=False`` and ``link_kind=injected_fake``, so this
    predicate returns false for it. Only a run through the gated real serial
    opener clears the bar, and even then flight/takeoff stay false.
    """

    evidence = summary.get("runtime_invocation_evidence")
    if not isinstance(evidence, Mapping):
        return False
    stage = summary.get("dispatch_stage_evidence")
    if not isinstance(stage, Mapping):
        return False
    verifier = stage.get("verifier_readback_observed")
    verifier = verifier if isinstance(verifier, Mapping) else {}
    return bool(
        evidence.get("backend_target") == REAL_HARDWARE_DISPATCH_BACKEND_TARGET
        and evidence.get("invocation_kind") == "mavlink"
        and evidence.get("link_kind") == LINK_KIND_REAL_SERIAL_PYMAVLINK
        and evidence.get("physical_execution_invoked") is True
        and evidence.get("flight_execution_invoked") is False
        and verifier.get("arm_status") == "accepted"
        and verifier.get("disarm_status") == "accepted"
        and verifier.get("arm_state_readback_observed") is True
        and verifier.get("disarm_state_readback_observed") is True
    )


__all__ = [
    "MISSIONOS_REAL_HARDWARE_DISPATCH_RUNTIME_OPT_IN_ENV",
    "REAL_HARDWARE_DISPATCH_BACKEND_TARGET",
    "REAL_HARDWARE_DISPATCH_RESPONSE_KIND",
    "REAL_HARDWARE_DISPATCH_STAGE_EVIDENCE_SCHEMA_VERSION",
    "MissionOSRealHardwareDispatchError",
    "invoke_missionos_real_hardware_dispatch_runtime",
    "real_hardware_dispatch_runtime_summary_supports_dispatch",
]
