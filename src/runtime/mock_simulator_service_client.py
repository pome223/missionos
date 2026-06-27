"""Client and attach helper for the Dockerized mock simulator adapter service.

This module consumes the HTTP service added for the dockerized mock simulator
adapter path. It deliberately treats the service response as untrusted: the
client revalidates the advertised adapter contract and every returned Mission
OS artifact before attaching anything to a task.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import json
from typing import Any
from urllib import error, request

from src.runtime.mock_simulator_adapter import (
    MockSimulatorGateResult,
    MockSimulatorReplayTrace,
    MockSimulatorReview,
    MockSimulatorScorecard,
    MockSimulatorState,
)
from src.runtime.physical_mission_replay import (
    SafetyGovernorDecisionArtifact,
    TelemetryHealthSnapshot,
)
from src.runtime.simulator_adapter_contract import (
    SimulatorAdapterContract,
    validate_simulator_adapter_safety_compatibility,
)
from src.runtime.task_store import TaskStore, get_task_store
from src.simulators.mock_adapter_server import MOCK_ADAPTER_RUN_RESULT_SCHEMA_VERSION


DEFAULT_MOCK_SIMULATOR_SERVICE_URL = "http://127.0.0.1:18888"

_REQUIRED_ARTIFACT_KEYS = {
    "simulator_adapter_contract",
    "mock_simulator_state",
    "telemetry_health_snapshot",
    "safety_governor_decision",
    "mock_simulator_replay_trace",
    "mock_simulator_scorecard",
    "mock_simulator_review",
    "mock_simulator_gate_result",
}
_FORBIDDEN_ATTACHMENT_KEYS = {
    "approval",
    "approval_package",
    "approved_skill",
    "capability_patch",
    "policy_patch",
    "promotion_package",
    "reuse_plan",
}


class MockSimulatorServiceClientError(RuntimeError):
    """Raised when the mock simulator service response cannot be trusted."""


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _load_json_response(response: Any) -> dict[str, Any]:
    payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise MockSimulatorServiceClientError("mock simulator service returned non-object JSON")
    return payload


def _get_json(base_url: str, path: str, *, timeout: float) -> dict[str, Any]:
    try:
        with request.urlopen(_join_url(base_url, path), timeout=timeout) as response:
            return _load_json_response(response)
    except error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise MockSimulatorServiceClientError(
            f"mock simulator service GET {path} failed with HTTP {exc.code}: {message}"
        ) from exc
    except OSError as exc:
        raise MockSimulatorServiceClientError(
            f"mock simulator service GET {path} failed: {exc}"
        ) from exc


def _post_json(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(
        _join_url(base_url, path),
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return _load_json_response(response)
    except error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise MockSimulatorServiceClientError(
            f"mock simulator service POST {path} failed with HTTP {exc.code}: {message}"
        ) from exc
    except OSError as exc:
        raise MockSimulatorServiceClientError(
            f"mock simulator service POST {path} failed: {exc}"
        ) from exc


def _require_false(payload: dict[str, Any], key: str, *, context: str) -> None:
    if payload.get(key) is not False:
        raise MockSimulatorServiceClientError(f"{context}.{key} must be false")


def _require_true(payload: dict[str, Any], key: str, *, context: str) -> None:
    if payload.get(key) is not True:
        raise MockSimulatorServiceClientError(f"{context}.{key} must be true")


def fetch_mock_simulator_service_contract(
    *,
    base_url: str = DEFAULT_MOCK_SIMULATOR_SERVICE_URL,
    timeout: float = 10.0,
) -> SimulatorAdapterContract:
    """Fetch and validate the service-advertised adapter contract."""

    contract_payload = _get_json(base_url, "/contract", timeout=timeout)
    return validate_simulator_adapter_safety_compatibility(contract_payload)


def validate_mock_simulator_service_run_result(
    result: dict[str, Any],
    *,
    expected_contract: SimulatorAdapterContract | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a `/run` response and return its artifact bundle.

    The returned artifact bundle is safe to attach to task artifacts only after
    this function succeeds.
    """

    if result.get("schema_version") != MOCK_ADAPTER_RUN_RESULT_SCHEMA_VERSION:
        raise MockSimulatorServiceClientError(
            "mock simulator service run result has unexpected schema_version"
        )
    if result.get("mode") != "dry_run_only":
        raise MockSimulatorServiceClientError("mock simulator service run mode must be dry_run_only")
    _require_true(result, "operator_approval_required", context="run_result")
    for key in (
        "live_execution_allowed",
        "physical_execution_invoked",
        "command_payload_allowed",
        "ros_dispatch_allowed",
        "mavlink_dispatch_allowed",
        "actuator_execution_allowed",
        "dispatch_implementation_present",
    ):
        _require_false(result, key, context="run_result")

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, dict):
        raise MockSimulatorServiceClientError("mock simulator service run result is missing artifacts")
    missing = sorted(_REQUIRED_ARTIFACT_KEYS.difference(artifacts))
    if missing:
        raise MockSimulatorServiceClientError(
            "mock simulator service artifacts are missing: " + ", ".join(missing)
        )
    forbidden = sorted(_FORBIDDEN_ATTACHMENT_KEYS.intersection(artifacts))
    if forbidden:
        raise MockSimulatorServiceClientError(
            "mock simulator service returned forbidden artifacts: " + ", ".join(forbidden)
        )

    artifact_contract = validate_simulator_adapter_safety_compatibility(
        artifacts["simulator_adapter_contract"]
    )
    if expected_contract is not None:
        expected = validate_simulator_adapter_safety_compatibility(expected_contract)
        if artifact_contract != expected:
            raise MockSimulatorServiceClientError(
                "mock simulator service artifact contract does not match fetched contract"
            )

    MockSimulatorState.model_validate(artifacts["mock_simulator_state"])
    TelemetryHealthSnapshot.model_validate(artifacts["telemetry_health_snapshot"])
    SafetyGovernorDecisionArtifact.model_validate(artifacts["safety_governor_decision"])
    MockSimulatorReplayTrace.model_validate(artifacts["mock_simulator_replay_trace"])
    MockSimulatorScorecard.model_validate(artifacts["mock_simulator_scorecard"])
    MockSimulatorReview.model_validate(artifacts["mock_simulator_review"])
    MockSimulatorGateResult.model_validate(artifacts["mock_simulator_gate_result"])
    return artifacts


def run_mock_simulator_service(
    *,
    base_url: str = DEFAULT_MOCK_SIMULATOR_SERVICE_URL,
    scenario_id: str = "mock_nominal",
    telemetry_case: str | None = None,
    now: datetime | str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Run the dry-run-only mock simulator service and validate its artifacts."""

    contract = fetch_mock_simulator_service_contract(base_url=base_url, timeout=timeout)
    payload: dict[str, Any] = {
        "scenario_id": scenario_id,
        "mode": "dry_run_only",
    }
    if telemetry_case is not None:
        payload["telemetry_case"] = telemetry_case
    if now is not None:
        payload["now"] = now.isoformat() if isinstance(now, datetime) else str(now)
    result = _post_json(base_url, "/run", payload, timeout=timeout)
    return validate_mock_simulator_service_run_result(
        result,
        expected_contract=contract,
    )


def attach_mock_simulator_service_run(
    task_id: str,
    *,
    base_url: str = DEFAULT_MOCK_SIMULATOR_SERVICE_URL,
    scenario_id: str = "mock_nominal",
    telemetry_case: str | None = None,
    now: datetime | str | None = None,
    timeout: float = 10.0,
    task_store_factory: Callable[[], TaskStore] | None = None,
) -> dict[str, Any]:
    """Fetch validated mock simulator service artifacts and attach them to a task."""

    store_factory = task_store_factory or get_task_store
    store = store_factory()
    current = store.get(task_id)
    if current is None:
        raise MockSimulatorServiceClientError(
            f"task {task_id} not found in task store; cannot attach mock simulator artifacts"
        )
    artifacts = run_mock_simulator_service(
        base_url=base_url,
        scenario_id=scenario_id,
        telemetry_case=telemetry_case,
        now=now,
        timeout=timeout,
    )
    updated = store.update(task_id, artifacts=artifacts)
    if updated is None:
        raise MockSimulatorServiceClientError(
            f"task {task_id} disappeared while attaching mock simulator artifacts"
        )
    return artifacts


__all__ = [
    "DEFAULT_MOCK_SIMULATOR_SERVICE_URL",
    "MockSimulatorServiceClientError",
    "attach_mock_simulator_service_run",
    "fetch_mock_simulator_service_contract",
    "run_mock_simulator_service",
    "validate_mock_simulator_service_run_result",
]
