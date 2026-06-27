#!/usr/bin/env python3
"""Opt-in PX4/Gazebo command-gated delivery phase smoke.

The smoke opens real UDP MAVLink sockets against a loopback PX4 endpoint,
requires operator-approved allowlisted dispatch results, observes the actual
Gazebo delivery-state world pose topic, and only completes the Mission OS task
when the command response evidence and Gazebo phase evidence both exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import socket
import threading
from tempfile import TemporaryDirectory

from src.runtime.gz_sim_log_collector import (
    delivery_phases_from_entity_poses,
    parse_gz_sim_entity_pose,
)
from src.runtime.px4_delivery_command_preflight import (
    PX4SimulationCommandKind,
    build_px4_simulation_command_preflight_artifacts,
)
from src.runtime.px4_gazebo_command_driven_delivery import (
    build_px4_gazebo_command_driven_phase_evidence,
    run_px4_gazebo_command_driven_delivery_task,
)
from src.runtime.px4_gazebo_delivery_world_profile import (
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_real_mavlink_transport import (
    decode_mavlink2_frame,
    encode_mavlink2_heartbeat,
    run_px4_real_mavlink_dispatch_result,
)
from src.runtime.px4_sitl_delivery_observation import (
    build_px4_sitl_delivery_observation_from_logs,
)
from src.runtime.task_store import TaskStore

from scripts.smoke_gazebo_entity_state_delivery import (
    _collect_pose_samples,
    _compose,
    _inspect_service,
    _stop_service,
    _wait_for_delivery_state_world,
)

OPT_IN_ENV = "RUN_PX4_GAZEBO_COMMAND_DRIVEN_PHASE_SMOKE"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PX4_SITL_LOGS = "\n".join(
    [
        "INFO  [px4] startup script: /bin/sh etc/init.d-posix/rcS 0",
        "INFO  [init] found model autostart file as SYS_AUTOSTART=10040",
        "INFO  [init] SIH simulator",
        "INFO  [simulator_sih] Simulation loop with 250 Hz",
        "INFO  [logger] logger started (mode=all)",
        "INFO  [px4] Startup script returned successfully",
    ]
)


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the PX4/Gazebo command-driven phase smoke."
        )


class FakePX4MAVLinkEndpoint:
    def __init__(self) -> None:
        self.received: list[dict] = []
        self.port: int | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "FakePX4MAVLinkEndpoint":
        self._thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("fake MAVLink endpoint did not start")
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"x", ("127.0.0.1", self.port or 9))
        self._thread.join(timeout=2)

    def _run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(0.2)
            self.port = int(sock.getsockname()[1])
            self._ready.set()
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                if data == b"x":
                    continue
                decoded = decode_mavlink2_frame(data)
                self.received.append(decoded)
                sock.sendto(
                    encode_mavlink2_heartbeat(
                        sequence=len(self.received),
                        system_id=1,
                        component_id=1,
                    ),
                    addr,
                )


def _preflight_artifacts():
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    observation = build_px4_sitl_delivery_observation_from_logs(
        PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
    )
    return build_px4_simulation_command_preflight_artifacts(
        profile=profile,
        observation=observation,
        operator_approval_performed=True,
        now=NOW,
    )


def _first_pose_sample_by_phase(pose_samples: list[str]) -> dict[str, str]:
    by_phase: dict[str, str] = {}
    for sample in pose_samples:
        pose = parse_gz_sim_entity_pose(sample)
        phases = delivery_phases_from_entity_poses([pose])
        for phase in phases:
            by_phase.setdefault(phase, sample)
    missing = [
        phase
        for phase in ("pickup", "enroute", "dropoff", "completed")
        if phase not in by_phase
    ]
    if missing:
        raise RuntimeError(f"missing Gazebo pose phases: {missing}")
    return by_phase


def main() -> int:
    _require_opt_in()
    _stop_service()
    try:
        _compose(
            "up",
            "-d",
            "--build",
            "boiled-claw-gz-sim-delivery-entity-state",
            timeout=240,
        )
        _wait_for_delivery_state_world()
        pose_samples = _collect_pose_samples()
        inspect_data = _inspect_service()
        pose_by_phase = _first_pose_sample_by_phase(pose_samples)
        preflight = _preflight_artifacts()
        with TemporaryDirectory() as tmp, FakePX4MAVLinkEndpoint() as endpoint:
            assert endpoint.port is not None
            store = TaskStore(f"{tmp}/tasks.db")
            task = store.create(
                kind="px4_gazebo_command_driven_delivery",
                title="PX4/Gazebo command-driven phase smoke",
                status="running",
                artifacts={"existing": {"case_id": "px4-gazebo-phase", "kept": True}},
            )
            phase_specs = (
                (PX4SimulationCommandKind.START_DELIVERY_MISSION, "pickup"),
                (PX4SimulationCommandKind.ADVANCE_DELIVERY_PHASE, "enroute"),
                (PX4SimulationCommandKind.ADVANCE_DELIVERY_PHASE, "dropoff"),
                (PX4SimulationCommandKind.ADVANCE_DELIVERY_PHASE, "completed"),
            )
            previous_sample: str | None = None
            evidence = []
            for kind, phase in phase_specs:
                dispatch = run_px4_real_mavlink_dispatch_result(
                    approval=preflight["px4_simulation_command_approval"],
                    allowlist=preflight["px4_simulation_command_allowlist"],
                    command_kind=kind,
                    endpoint_port=endpoint.port,
                    opt_in=True,
                    timeout_seconds=1.0,
                    now=NOW,
                )
                sample = pose_by_phase[phase]
                evidence.append(
                    build_px4_gazebo_command_driven_phase_evidence(
                        real_dispatch_result=dispatch,
                        mission_phase=phase,
                        previous_gazebo_pose_sample=previous_sample,
                        gazebo_pose_sample=sample,
                        now=NOW,
                    )
                )
                previous_sample = sample
            updated = run_px4_gazebo_command_driven_delivery_task(
                task["task_id"],
                phase_evidence=evidence,
                now=NOW,
                task_store_factory=lambda: store,
            )
        runner = updated["artifacts"]["px4_gazebo_command_driven_runner_result"]
        summary = {
            "task_status": updated["status"],
            "existing_artifacts_retained": updated["artifacts"]["existing"]["kept"],
            "phase_evidence_count": len(
                updated["artifacts"]["px4_gazebo_command_driven_phase_evidence"]
            ),
            "observed_delivery_phases": runner["observed_delivery_phases"],
            "final_status": runner["final_status"],
            "completion_basis": runner["completion_basis"],
            "completion_mode": runner["completion_mode"],
            "px4_command_response_required": runner["px4_command_response_required"],
            "gazebo_pose_phase_required": runner["gazebo_pose_phase_required"],
            "delivery_phase_command_executed": runner[
                "delivery_phase_command_executed"
            ],
            "simulation_actuator_effect_observed": runner[
                "simulation_actuator_effect_observed"
            ],
            "simulation_mavlink_dispatch_allowed": runner[
                "simulation_mavlink_dispatch_allowed"
            ],
            "simulation_actuator_effect_allowed": runner[
                "simulation_actuator_effect_allowed"
            ],
            "physical_actuator_execution_allowed": runner[
                "physical_actuator_execution_allowed"
            ],
            "hardware_target_allowed": runner["hardware_target_allowed"],
            "physical_execution_invoked": runner["physical_execution_invoked"],
            "px4_gazebo_coupled_motion_observed": runner[
                "px4_gazebo_coupled_motion_observed"
            ],
            "actual_gazebo_container_running": inspect_data["State"]["Running"],
            "actual_gazebo_pose_samples": len(pose_samples),
            "real_udp_mavlink_frames_received_by_endpoint": len(endpoint.received),
            "px4_endpoint_kind": "fake_loopback",
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        assert summary["task_status"] == "completed"
        assert summary["phase_evidence_count"] == 4
        assert summary["observed_delivery_phases"] == [
            "pickup",
            "enroute",
            "dropoff",
            "completed",
        ]
        assert (
            summary["completion_basis"]
            == "command_response_and_independent_gazebo_pose_phase"
        )
        assert summary["completion_mode"] == "command_gated_observation_completed"
        assert summary["px4_command_response_required"] is True
        assert summary["gazebo_pose_phase_required"] is True
        assert summary["delivery_phase_command_executed"] is False
        assert summary["simulation_actuator_effect_observed"] is False
        assert summary["simulation_mavlink_dispatch_allowed"] is True
        assert summary["simulation_actuator_effect_allowed"] is True
        assert summary["physical_actuator_execution_allowed"] is False
        assert summary["hardware_target_allowed"] is False
        assert summary["physical_execution_invoked"] is False
        assert summary["actual_gazebo_container_running"] is True
        assert summary["real_udp_mavlink_frames_received_by_endpoint"] == 4
        return 0
    finally:
        _stop_service()


if __name__ == "__main__":
    raise SystemExit(main())
