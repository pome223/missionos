from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import execution
from src.runtime.px4_gazebo_route.observation import distance_to_segment_xy


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


class FakeStdin:
    def __init__(self) -> None:
        self.value = ""
        self.closed = False

    def write(self, value: str) -> None:
        self.value += value

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(
        self,
        *,
        polls: list[int | None],
        stdout: str,
        stderr: str = "",
    ) -> None:
        self._polls = list(polls)
        self._stdout = stdout
        self._stderr = stderr
        self.stdin: FakeStdin | None = FakeStdin()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        value = self._polls.pop(0) if self._polls else self.returncode
        if value is not None:
            self.returncode = value
        return value

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def communicate(self, *, timeout: int) -> tuple[str, str]:
        assert timeout == 5
        return self._stdout, self._stderr


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


def test_route_monitor_returns_helper_result_after_bounded_pose_sampling() -> None:
    process = FakeProcess(
        polls=[None, 0],
        stdout=json.dumps({"mode": "route", "sent": True}),
    )
    popen_calls: list[dict[str, Any]] = []
    observed_rows: list[tuple[str, dict[str, float], int]] = []

    def popen_factory(command: list[str], **kwargs: Any) -> FakeProcess:
        popen_calls.append({"command": command, **kwargs})
        return process

    result = execution.run_route_with_monitor(
        target_x=2.0,
        target_y=0.0,
        target_z=-2.5,
        expected_target_x=2.0,
        expected_target_y=0.0,
        pickup_pose={"x": 0.0, "y": 0.0, "z": 0.0},
        altitude_max_m=-2.5,
        max_pose_deviation_xy_m=1.0,
        max_pose_deviation_z_m=1.0,
        duration_seconds=0.2,
        container_name="fixture-container",
        helper_source="fixture route helper",
        pose_sampler=lambda: {"x": 1.0, "y": 0.0, "z": -2.5},
        append_pose_row=lambda phase, sample, *, sample_index: observed_rows.append(
            (phase, sample, sample_index)
        ),
        distance_to_segment=distance_to_segment_xy,
        popen_factory=popen_factory,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert result["sent"] is True
    assert result["pose_deviation_aborted"] is False
    assert result["route_monitor_sample_count"] == 1
    assert result["feed_forward_phase_schedule"] == "full_then_linear_ramp_down"
    assert observed_rows == [
        ("route", {"x": 1.0, "y": 0.0, "z": -2.5}, 0)
    ]
    assert popen_calls[0]["command"][3] == "fixture-container"
    assert process.stdin is None


def test_route_monitor_stops_stream_before_deviation_recovery() -> None:
    process = FakeProcess(polls=[None], stdout="", stderr="terminated")
    recovery_calls: list[str] = []

    result = execution.run_route_with_monitor(
        target_x=2.0,
        target_y=0.0,
        target_z=-2.5,
        expected_target_x=2.0,
        expected_target_y=0.0,
        pickup_pose={"x": 0.0, "y": 0.0, "z": 0.0},
        altitude_max_m=-2.5,
        max_pose_deviation_xy_m=0.5,
        max_pose_deviation_z_m=0.5,
        duration_seconds=0.2,
        container_name="fixture-container",
        helper_source="fixture route helper",
        pose_sampler=lambda: {"x": 0.5, "y": 2.0, "z": -2.5},
        append_pose_row=lambda *_args, **_kwargs: None,
        distance_to_segment=distance_to_segment_xy,
        on_deviation=lambda: recovery_calls.append("called") or {"status": "held"},
        popen_factory=lambda *_args, **_kwargs: process,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert process.terminated is True
    assert recovery_calls == ["called"]
    assert result["sent"] is False
    assert result["pose_deviation_aborted"] is True
    assert result["route_stream_terminated_before_recovery_dispatch"] is True
    assert result["route_stream_stop_reason"] == "pose_deviation"
    assert result["recovery_payload"] == {"status": "held"}


def test_route_monitor_timeout_terminates_without_sampling() -> None:
    process = FakeProcess(polls=[None], stdout="")
    clock = iter([0.0, 2.0])

    with pytest.raises(RuntimeError, match="timed out while monitoring pose"):
        execution.run_route_with_monitor(
            target_x=2.0,
            target_y=0.0,
            target_z=-2.5,
            expected_target_x=2.0,
            expected_target_y=0.0,
            pickup_pose={"x": 0.0, "y": 0.0, "z": 0.0},
            altitude_max_m=-2.5,
            max_pose_deviation_xy_m=1.0,
            max_pose_deviation_z_m=1.0,
            duration_seconds=0.2,
            container_name="fixture-container",
            helper_source="fixture route helper",
            pose_sampler=lambda: pytest.fail("pose must not be sampled after timeout"),
            append_pose_row=lambda *_args, **_kwargs: None,
            distance_to_segment=distance_to_segment_xy,
            timeout=1,
            popen_factory=lambda *_args, **_kwargs: process,
            monotonic=lambda: next(clock),
            sleep=lambda _seconds: None,
        )

    assert process.terminated is True
