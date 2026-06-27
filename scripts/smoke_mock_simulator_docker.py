#!/usr/bin/env python3
"""Docker smoke for the mock simulator adapter service.

This script starts the optional `simulator` compose profile, exercises the real
HTTP service boundary, attaches validated artifacts to a temporary TaskStore,
and stops the mock simulator container when finished.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, request


ROOT_DIR = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:18888"
SERVICE_NAME = "boiled-claw-mock-simulator"


def _run_command(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT_DIR,
        check=True,
        capture_output=capture,
        text=True,
    )


def _get_json(path: str) -> dict:
    with request.urlopen(BASE_URL + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = request.Request(
        BASE_URL + path,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_health(*, timeout_seconds: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            payload = _get_json("/health")
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = exc
            time.sleep(1)
            continue
        if payload.get("status") == "ok":
            return payload
        time.sleep(1)
    raise RuntimeError(f"mock simulator service did not become healthy: {last_error}")


def _exercise_service() -> dict:
    from src.runtime.mock_simulator_service_client import attach_mock_simulator_service_run
    from src.runtime.task_store import TaskStore

    health = _get_json("/health")
    contract = _get_json("/contract")
    nominal = _post_json(
        "/run",
        {
            "scenario_id": "docker-smoke-nominal",
            "mode": "dry_run_only",
            "now": "2026-04-30T01:02:03+00:00",
        },
    )
    unsafe = _post_json(
        "/run",
        {
            "scenario_id": "docker-smoke-unsafe",
            "mode": "dry_run_only",
            "telemetry_case": "unsafe",
            "now": "2026-04-30T01:02:03+00:00",
        },
    )
    try:
        _post_json(
            "/run",
            {
                "scenario_id": "docker-smoke-reject-command",
                "mode": "dry_run_only",
                "metadata": {"history": [{"RosTopic": "/cmd_vel"}]},
            },
        )
    except error.HTTPError as exc:
        reject_status = exc.code
        reject_payload = json.loads(exc.read().decode("utf-8"))
    else:  # pragma: no cover - fail path
        raise AssertionError("command-like request should have been rejected")

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="docker mock simulator smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        artifacts = attach_mock_simulator_service_run(
            task["task_id"],
            base_url=BASE_URL,
            scenario_id="docker-client-smoke",
            now="2026-04-30T01:02:03+00:00",
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])
        assert stored is not None
        assert stored["status"] == "running"
        assert stored["artifacts"]["existing"] == {"kept": True}
        assert "approval" not in stored["artifacts"]
        assert "promotion_package" not in stored["artifacts"]
        assert "reuse_plan" not in stored["artifacts"]

    assert health["live_execution_allowed"] is False
    assert health["physical_execution_invoked"] is False
    assert health["dispatch_implementation_present"] is False
    assert contract["adapter_id"] == "mock_physical_simulator.v1"
    assert contract["supports_live_execution"] is False
    assert contract["supports_physical_execution"] is False
    assert contract["supports_ros_dispatch"] is False
    assert nominal["artifacts"]["mock_simulator_gate_result"]["passed"] is True
    assert unsafe["artifacts"]["mock_simulator_gate_result"]["passed"] is False
    assert "mock_simulator_governor_blocked" in unsafe["artifacts"][
        "mock_simulator_gate_result"
    ]["blocked_reasons"]
    assert reject_status == 400
    assert "command-like or dispatch-like key" in reject_payload["error"]
    assert artifacts["mock_simulator_replay_trace"]["live_execution_allowed"] is False
    assert artifacts["mock_simulator_replay_trace"]["physical_execution_invoked"] is False

    return {
        "service": SERVICE_NAME,
        "health_status": health["status"],
        "contract_adapter_id": contract["adapter_id"],
        "nominal_gate_passed": nominal["artifacts"]["mock_simulator_gate_result"][
            "passed"
        ],
        "unsafe_gate_status": unsafe["artifacts"]["mock_simulator_gate_result"][
            "status"
        ],
        "reject_status": reject_status,
        "attached_task_status": stored["status"],
        "existing_artifact_kept": stored["artifacts"]["existing"]["kept"],
        "attached_gate_passed": artifacts["mock_simulator_gate_result"]["passed"],
        "live_execution_allowed": artifacts["mock_simulator_replay_trace"][
            "live_execution_allowed"
        ],
        "physical_execution_invoked": artifacts["mock_simulator_replay_trace"][
            "physical_execution_invoked"
        ],
    }


def _stop_service() -> None:
    compose = ["docker", "compose", "--profile", "simulator"]
    subprocess.run(
        [*compose, "stop", SERVICE_NAME],
        cwd=ROOT_DIR,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [*compose, "rm", "-f", SERVICE_NAME],
        cwd=ROOT_DIR,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Start the service without rebuilding the image.",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave the mock simulator container running after the smoke.",
    )
    args = parser.parse_args()

    compose = ["docker", "compose", "--profile", "simulator"]
    up_command = [*compose, "up", "-d"]
    if not args.skip_build:
        up_command.append("--build")
    up_command.append(SERVICE_NAME)

    try:
        _run_command(up_command)
        _wait_for_health()
        summary = _exercise_service()
        print(json.dumps(summary, indent=2, sort_keys=True))
    finally:
        if not args.keep_running:
            _stop_service()
    return 0


if __name__ == "__main__":
    sys.exit(main())
