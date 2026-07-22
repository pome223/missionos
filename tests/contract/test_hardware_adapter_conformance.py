from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any

import pytest
from pydantic import ValidationError

from src.runtime.hardware_adapter_contract import (
    HardwareActionKind,
    HardwareAdapterEvidence,
    HardwareOperatorApproval,
)
from src.runtime.hardware_adapter_registrations import (
    register_ros2_nav2_adapter,
    unitree_adapter_factory,
)
from src.runtime.hardware_adapter_runtime import (
    HARDWARE_ADAPTER_APPROVAL_TTL,
    HardwareAdapterRegistry,
    HardwareAdapterRegistryError,
    HardwareAdapterRuntimeRequest,
    abort_hardware_adapter_action,
    dispatch_hardware_adapter_action,
    prepare_hardware_adapter_action,
    verify_hardware_adapter_outcome,
)
from src.runtime.ros2_nav2_hardware_adapter import ROS2_NAV2_HARDWARE_ADAPTER_ID
from src.runtime.unitree_hardware_adapter import (
    UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
    UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV,
)


class ConformanceClient:
    """One deterministic client surface shared by both adapter registrations."""

    def __init__(
        self,
        *,
        progress_observed: bool = True,
        runtime_invocation_present: bool = True,
        raise_on_dispatch: bool = False,
    ) -> None:
        self.progress_observed = progress_observed
        self.runtime_invocation_present = runtime_invocation_present
        self.raise_on_dispatch = raise_on_dispatch
        self.dispatch_calls = 0

    def _dispatch(self) -> dict[str, Any]:
        self.dispatch_calls += 1
        if self.raise_on_dispatch:
            raise RuntimeError("conformance adapter failure")
        return {"ack_status": "accepted", "ack_source": "conformance_client"}

    def send_bounded_local_move(self, _move: Any) -> dict[str, Any]:
        return self._dispatch()

    def send_goal_pose(self, _goal: Any) -> dict[str, Any]:
        return self._dispatch()

    def hold(self) -> dict[str, Any]:
        return self._dispatch()

    def safe_stop(self) -> dict[str, Any]:
        return self._dispatch()

    def cancel_goal(self) -> dict[str, Any]:
        return self._dispatch()

    def read_state(self) -> dict[str, Any]:
        return {
            "pose_observed": True,
            "robot_motion_observed": self.progress_observed,
            "odom_delta_m": 0.25 if self.progress_observed else 0.0,
        }

    def read_progress(self) -> dict[str, Any]:
        return {
            "runtime_progress_observed": self.progress_observed,
            "completion_observed": self.progress_observed,
            "robot_motion_observed": self.progress_observed,
            "move_completed": self.progress_observed,
            "unitree_status": "succeeded" if self.progress_observed else "accepted",
            "nav2_status": "succeeded" if self.progress_observed else "accepted",
        }

    def collect_runtime_invocation_evidence(self) -> tuple[dict[str, Any], ...]:
        if not self.runtime_invocation_present or not self.dispatch_calls:
            return ()
        now = datetime.now(timezone.utc).isoformat()
        stdout = json.dumps(
            {"dispatch_calls": self.dispatch_calls, "client": "conformance"},
            sort_keys=True,
        )
        return (
            {
                "schema_version": "runtime_invocation_evidence.v1",
                "invocation_kind": "subprocess",
                "invocation_target": "adapter_conformance_fixture",
                "invocation_started_at": now,
                "invocation_completed_at": now,
                "invocation_stdout_sha256": sha256(stdout.encode()).hexdigest(),
                "invocation_stderr_sha256": sha256(b"").hexdigest(),
                "invocation_stdout_preimage": stdout,
                "invocation_stderr_preimage": "",
                "invocation_exit_code": 0,
            },
        )


@dataclass
class AdapterConformanceCase:
    adapter_id: str
    action_kind: HardwareActionKind
    parameters: dict[str, Any]
    registry: HardwareAdapterRegistry
    client: ConformanceClient

    def request(self, **overrides: Any) -> HardwareAdapterRuntimeRequest:
        payload = {
            "adapter_id": self.adapter_id,
            "missionos_action_ref": f"conformance_action:{self.adapter_id}",
            "action_kind": self.action_kind,
            "adapter_parameters": self.parameters,
            "execution_mode": "sim",
            "opt_in": True,
            "telemetry_fresh": True,
            "heartbeat_alive": True,
            "geofence_satisfied": True,
            "operating_volume_satisfied": True,
        }
        payload.update(overrides)
        return HardwareAdapterRuntimeRequest(**payload)

    def approval(
        self,
        request: HardwareAdapterRuntimeRequest,
        **overrides: Any,
    ) -> HardwareOperatorApproval:
        preparation = prepare_hardware_adapter_action(
            registry=self.registry,
            request=request,
        )
        approved_at = datetime.now(timezone.utc)
        payload = {
            "operator_approval_ref": f"approval:{self.adapter_id}",
            "approved_adapter_id": self.adapter_id,
            "approval_actor": "conformance-operator",
            "approved_preparation_ref": preparation.preparation_ref,
            "approved_preparation_sha256": preparation.preparation_sha256,
            "approval_timestamp": approved_at,
            "approval_expires_at": approved_at + HARDWARE_ADAPTER_APPROVAL_TTL,
            "approved_action_ref": request.missionos_action_ref,
            "approved_action_kind": request.action_kind,
        }
        payload.update(overrides)
        return HardwareOperatorApproval(**payload)


@pytest.fixture(params=("ros2_nav2", "unitree_mujoco"))
def adapter_case(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    client = ConformanceClient()
    registry = HardwareAdapterRegistry()
    if request.param == "ros2_nav2":
        register_ros2_nav2_adapter(registry, client_factory=lambda: client)
        return AdapterConformanceCase(
            adapter_id=ROS2_NAV2_HARDWARE_ADAPTER_ID,
            action_kind=HardwareActionKind.NAV2_GOAL_POSE,
            parameters={"x_m": 0.25, "y_m": 0.0},
            registry=registry,
            client=client,
        )
    monkeypatch.setenv(UNITREE_MUJOCO_HARDWARE_ADAPTER_OPT_IN_ENV, "1")
    registry.register(
        UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        unitree_adapter_factory(client_factory=lambda: client),
    )
    return AdapterConformanceCase(
        adapter_id=UNITREE_MUJOCO_HARDWARE_ADAPTER_ID,
        action_kind=HardwareActionKind.BOUNDED_LOCAL_MOVE,
        parameters={"forward_m": 0.25, "lateral_m": 0.0},
        registry=registry,
        client=client,
    )


def test_conformance_proposal_alone_never_dispatches(
    adapter_case: AdapterConformanceCase,
) -> None:
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=adapter_case.request(),
    )

    assert preparation.dispatch_request_sent is False
    assert preparation.dispatch_authority_created is False
    assert adapter_case.client.dispatch_calls == 0


def test_conformance_missing_or_mismatched_approval_blocks_dispatch(
    adapter_case: AdapterConformanceCase,
) -> None:
    request = adapter_case.request()
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    missing = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=None,
    )
    mismatched = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(
            request,
            approved_action_ref="different-action",
        ),
    )
    wrong_adapter = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(
            request,
            approved_adapter_id="different-adapter.v1",
        ),
    )
    wrong_preparation = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(
            request,
            approved_preparation_ref="different-preparation",
        ),
    )

    assert missing.dispatch_request_sent is False
    assert mismatched.dispatch_request_sent is False
    assert wrong_adapter.dispatch_request_sent is False
    assert wrong_preparation.dispatch_request_sent is False
    assert adapter_case.client.dispatch_calls == 0
    assert "operator_approval_missing" in missing.verification_verdict[
        "blocking_reasons"
    ]
    assert "operator_approval_action_ref_mismatch" in mismatched.verification_verdict[
        "blocking_reasons"
    ]
    assert "operator_approval_adapter_mismatch" in wrong_adapter.verification_verdict[
        "blocking_reasons"
    ]
    assert "operator_approval_preparation_ref_mismatch" in (
        wrong_preparation.verification_verdict["blocking_reasons"]
    )


def test_conformance_stale_or_unbounded_approval_blocks_dispatch(
    adapter_case: AdapterConformanceCase,
) -> None:
    request = adapter_case.request()
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    stale_at = datetime.now(timezone.utc) - HARDWARE_ADAPTER_APPROVAL_TTL - timedelta(
        seconds=1
    )
    stale = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(
            request,
            approval_timestamp=stale_at,
            approval_expires_at=stale_at + HARDWARE_ADAPTER_APPROVAL_TTL,
        ),
    )
    no_expiry = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(request, approval_expires_at=None),
    )

    assert stale.dispatch_request_sent is False
    assert no_expiry.dispatch_request_sent is False
    assert adapter_case.client.dispatch_calls == 0
    assert "operator_approval_stale" in stale.verification_verdict[
        "blocking_reasons"
    ]
    assert "operator_approval_expiry_missing" in no_expiry.verification_verdict[
        "blocking_reasons"
    ]


@pytest.mark.parametrize(
    "missing_field",
    (
        "telemetry_fresh",
        "heartbeat_alive",
        "geofence_satisfied",
        "operating_volume_satisfied",
    ),
)
def test_runtime_request_requires_explicit_safety_telemetry(
    adapter_case: AdapterConformanceCase,
    missing_field: str,
) -> None:
    payload = adapter_case.request().model_dump()
    payload.pop(missing_field)

    with pytest.raises(ValidationError):
        HardwareAdapterRuntimeRequest.model_validate(payload)


def test_conformance_ack_without_progress_is_not_completion(
    adapter_case: AdapterConformanceCase,
) -> None:
    adapter_case.client.progress_observed = False
    request = adapter_case.request()
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    result = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(request),
    )

    assert result.command_ack_observed is True
    assert result.runtime_progress_observed is False
    assert result.completion_claimed is False
    assert result.verification_verdict["adapter_action_verified"] is False


def test_conformance_missing_runtime_invocation_cannot_verify_completion(
    adapter_case: AdapterConformanceCase,
) -> None:
    adapter_case.client.runtime_invocation_present = False
    request = adapter_case.request()
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    result = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(request),
    )

    assert result.adapter_evidence["completion_claimed"] is True
    assert result.completion_claimed is False
    assert "runtime_invocation_evidence_missing" in result.verification_verdict[
        "blocking_reasons"
    ]


def test_conformance_fresh_runtime_evidence_verifies_adapter_action_only(
    adapter_case: AdapterConformanceCase,
) -> None:
    request = adapter_case.request()
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    result = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(request),
    )

    assert result.dispatch_request_sent is True
    assert result.command_ack_observed is True
    assert result.runtime_progress_observed is True
    assert result.completion_claimed is True
    assert result.verification_verdict["adapter_action_verified"] is True
    assert result.verification_verdict["completion_scope"] in {
        "loopback_action",
        "sim_action",
    }
    assert result.physical_execution_invoked is False
    invocation = result.runtime_invocation_evidence[0]
    assert invocation["missionos_adapter_id"] == adapter_case.adapter_id
    assert invocation["missionos_action_ref"] == request.missionos_action_ref
    assert invocation["missionos_preparation_ref"] == preparation.preparation_ref
    assert invocation["missionos_preparation_sha256"] == (
        preparation.preparation_sha256
    )
    assert invocation["operator_approval_ref"] == result.operator_approval[
        "operator_approval_ref"
    ]
    assert "adapter_action_verdict_not_mission_completion" in (
        result.verification_verdict["limitations"]
    )


def test_conformance_mixed_runtime_invocation_binding_cannot_verify(
    adapter_case: AdapterConformanceCase,
) -> None:
    request = adapter_case.request()
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    approval = adapter_case.approval(request)
    result = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=approval,
    )
    tampered = [dict(item) for item in result.runtime_invocation_evidence]
    tampered[0]["missionos_preparation_ref"] = "another-preparation"
    verdict = verify_hardware_adapter_outcome(
        request=request,
        approval_scope_valid=True,
        evidence=HardwareAdapterEvidence.model_validate(result.adapter_evidence),
        runtime_invocation_evidence=tampered,
        preparation=preparation,
        operator_approval_ref=approval.operator_approval_ref,
    )

    assert verdict.adapter_action_verified is False
    assert verdict.completion_claimed is False
    assert "runtime_invocation_missionos_preparation_ref_mismatch" in (
        verdict.blocking_reasons
    )


def test_conformance_stale_telemetry_fails_closed(
    adapter_case: AdapterConformanceCase,
) -> None:
    request = adapter_case.request(telemetry_fresh=False)
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    result = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(request),
    )

    assert preparation.preflight["preflight_status"] == "blocked"
    assert result.dispatch_request_sent is False
    assert result.completion_claimed is False


def test_conformance_unknown_capability_fails_closed(
    adapter_case: AdapterConformanceCase,
) -> None:
    request = adapter_case.request(
        action_kind=HardwareActionKind.RAW_MOTOR,
        adapter_parameters={},
    )
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    result = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(request),
    )

    assert preparation.preflight["preflight_status"] == "blocked"
    assert result.dispatch_status == "blocked"
    assert result.dispatch_request_sent is False
    assert result.completion_claimed is False


def test_conformance_adapter_failure_records_unknown_dispatch_state(
    adapter_case: AdapterConformanceCase,
) -> None:
    adapter_case.client.raise_on_dispatch = True
    request = adapter_case.request()
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    result = dispatch_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(request),
    )

    assert result.dispatch_status == "unknown"
    assert result.dispatch_request_sent is None
    assert result.completion_claimed is False
    assert "dispatch_state_unknown" in result.verification_verdict["blocking_reasons"]


def test_conformance_factory_failure_is_blocked_before_dispatch(
    adapter_case: AdapterConformanceCase,
) -> None:
    request = adapter_case.request()
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    failing_registry = HardwareAdapterRegistry()

    def fail_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("factory unavailable")

    failing_registry.register(adapter_case.adapter_id, fail_factory)
    result = dispatch_hardware_adapter_action(
        registry=failing_registry,
        preparation=preparation,
        approval=adapter_case.approval(request),
    )

    assert result.dispatch_status == "blocked"
    assert result.dispatch_request_sent is False
    assert result.completion_claimed is False
    assert "dispatch_not_attempted" in result.verification_verdict["blocking_reasons"]


def test_conformance_abort_is_recorded_separately_from_completion(
    adapter_case: AdapterConformanceCase,
) -> None:
    request = adapter_case.request()
    preparation = prepare_hardware_adapter_action(
        registry=adapter_case.registry,
        request=request,
    )
    result = abort_hardware_adapter_action(
        registry=adapter_case.registry,
        preparation=preparation,
        approval=adapter_case.approval(request),
    )

    assert result.adapter_evidence["safe_stop_requested"] is True
    assert result.adapter_evidence["abort_requested"] is True
    assert result.dispatch_request_sent is False
    assert result.completion_claimed is False


def test_unregistered_adapter_fails_closed() -> None:
    registry = HardwareAdapterRegistry()
    request = HardwareAdapterRuntimeRequest(
        adapter_id="unregistered_adapter.v1",
        missionos_action_ref="unregistered-action",
        action_kind=HardwareActionKind.HOLD,
        telemetry_fresh=True,
        heartbeat_alive=True,
        geofence_satisfied=True,
        operating_volume_satisfied=True,
    )

    with pytest.raises(HardwareAdapterRegistryError, match="adapter_not_registered"):
        prepare_hardware_adapter_action(registry=registry, request=request)
