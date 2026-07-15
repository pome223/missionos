from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import execution


class FakeRunner:
    def __init__(self, *results: subprocess.CompletedProcess[str]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        command: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(
            {
                "command": command,
                "check": check,
                "input_text": input_text,
                "timeout": timeout,
            }
        )
        return self.results.pop(0)


def _completed(
    *,
    returncode: int = 0,
    payload: dict[str, Any] | None = None,
    stdout: str | None = None,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=(json.dumps(payload) if stdout is None else stdout),
        stderr=stderr,
    )


def test_send_embedded_helper_uses_explicit_runner_and_container() -> None:
    runner = FakeRunner(_completed(payload={"mode": "arm", "sent": True}))
    result = execution.send_embedded_helper(
        "arm",
        runner=runner,
        container_name="fixture-container",
        helper_source="fixture route helper",
        timeout=9,
    )

    assert result == {"mode": "arm", "sent": True}
    assert runner.calls == [
        {
            "command": [
                "docker",
                "exec",
                "-i",
                "fixture-container",
                "python3",
                "-",
                "arm",
            ],
            "check": True,
            "input_text": "fixture route helper",
            "timeout": 9,
        }
    ]


def test_heartbeat_observer_preserves_read_only_claims() -> None:
    runner = FakeRunner(
        _completed(
            payload={
                "observer_status": "completed",
                "heartbeat_count": 3,
                "observer_sent_packets": True,
                "packet_drop_performed": True,
            }
        )
    )
    result = execution.observe_mavlink_heartbeat_gap(
        runner=runner,
        container_name="fixture-container",
        helper_source="fixture observer",
        local_port=15550,
        duration_seconds=0.25,
        gap_threshold_seconds=0.1,
    )

    assert result["observer_status"] == "completed"
    assert result["heartbeat_count"] == 3
    assert result["observer_sent_packets"] is False
    assert result["packet_drop_performed"] is False
    assert runner.calls[0]["check"] is False
    assert runner.calls[0]["timeout"] == 5


@pytest.mark.parametrize(
    ("completed", "expected_status"),
    [
        (_completed(returncode=1, stdout="", stderr="observer failed"), "failed"),
        (_completed(stdout="not-json"), "invalid_output"),
    ],
)
def test_heartbeat_observer_fails_closed(
    completed: subprocess.CompletedProcess[str],
    expected_status: str,
) -> None:
    result = execution.observe_mavlink_heartbeat_gap(
        runner=FakeRunner(completed),
        container_name="fixture-container",
        helper_source="fixture observer",
        local_port=15550,
        duration_seconds=0.25,
        gap_threshold_seconds=0.1,
    )

    assert result["observer_status"] == expected_status
    assert result["source"] == "udp://127.0.0.1:15550"
    assert result["heartbeat_gap_observed"] is False
    assert result["observer_sent_packets"] is False
    assert result["packet_drop_performed"] is False


def test_bounded_link_loss_uses_explicit_ports_and_never_claims_failsafe() -> None:
    runner = FakeRunner(
        _completed(
            payload={
                "applicator_status": "completed",
                "endpoint_stop_performed": True,
                "endpoint_restart_performed": True,
                "observer_sent_packets": True,
                "packet_drop_performed": True,
                "rf_link_loss_claimed": True,
                "vehicle_failsafe_claimed": True,
            }
        )
    )
    result = execution.apply_bounded_mavlink_link_loss(
        runner=runner,
        container_name="fixture-container",
        helper_source="fixture applicator",
        route_px4_port=14600,
        route_local_port=14650,
        emergency_px4_port=14601,
        emergency_local_port=14651,
        restart_emergency=False,
        duration_seconds=0.25,
        gap_threshold_seconds=0.1,
    )

    assert result["applicator_status"] == "completed"
    assert result["endpoint_stop_performed"] is True
    assert result["endpoint_restart_performed"] is True
    assert result["observer_sent_packets"] is False
    assert result["packet_drop_performed"] is False
    assert result["rf_link_loss_claimed"] is False
    assert result["vehicle_failsafe_claimed"] is False
    assert runner.calls[0]["command"][-7:] == [
        "0.25",
        "0.1",
        "14600",
        "14650",
        "14601",
        "14651",
        "0",
    ]


@pytest.mark.parametrize(
    ("completed", "expected_status"),
    [
        (_completed(returncode=1, stdout="", stderr="link failed"), "failed"),
        (_completed(stdout="not-json"), "invalid_output"),
    ],
)
def test_bounded_link_loss_fails_closed(
    completed: subprocess.CompletedProcess[str],
    expected_status: str,
) -> None:
    result = execution.apply_bounded_mavlink_link_loss(
        runner=FakeRunner(completed),
        container_name="fixture-container",
        helper_source="fixture applicator",
        route_px4_port=14600,
        route_local_port=14650,
        emergency_px4_port=14601,
        emergency_local_port=14651,
        restart_emergency=True,
    )

    assert result["applicator_status"] == expected_status
    assert result["endpoint_stop_performed"] is False
    assert result["endpoint_restart_performed"] is False
    assert result["rf_link_loss_claimed"] is False
    assert result["vehicle_failsafe_claimed"] is False


def test_legacy_send_helper_delegates_to_packaged_execution(monkeypatch: Any) -> None:
    runner = FakeRunner(_completed(payload={"mode": "land", "sent": True}))
    monkeypatch.setattr(route_entrypoint, "_run", runner)

    assert route_entrypoint._send_helper("land", timeout=7) == {
        "mode": "land",
        "sent": True,
    }
    assert runner.calls[0]["command"][3] == route_entrypoint.CONTAINER_NAME
    assert runner.calls[0]["input_text"] == route_entrypoint.MAVLINK_ROUTE_HELPER
    assert runner.calls[0]["timeout"] == 7
