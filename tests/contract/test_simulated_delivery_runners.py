from datetime import timedelta
from pathlib import Path

from src.runtime.px4_controlled_delivery_runner import (
    run_px4_controlled_gazebo_delivery_mission_v0_task,
)
from src.runtime.px4_delivery_command_preflight import (
    build_px4_simulation_command_preflight_artifacts,
)
from src.runtime.px4_gazebo_delivery_world_profile import (
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_gazebo_sitl_telemetry_run import (
    build_px4_gazebo_sitl_telemetry_run,
)
from src.runtime.px4_gazebo_telemetry import sanitize_px4_gazebo_telemetry_sample
from src.runtime.px4_sitl_delivery_observation import (
    build_px4_sitl_delivery_observation_from_logs,
)
from src.runtime.simulated_delivery_episode import (
    SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION,
    attach_simulated_delivery_episode_from_sitl_telemetry_run,
)
from src.runtime.simulated_delivery_runner import (
    create_and_run_simulated_delivery_task_v0,
)
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_artifact_chain import (
    NOW,
    build_completed_delivery_contract,
    build_delivery_contract,
)


PX4_SITL_LOGS = "\n".join(
    (
        "INFO  [px4] startup script: /bin/sh etc/init.d-posix/rcS 0",
        "INFO  [init] found model autostart file as SYS_AUTOSTART=10040",
        "INFO  [init] SIH simulator",
        "INFO  [simulator_sih] Simulation loop with 250 Hz",
        "INFO  [logger] logger started (mode=all)",
        "INFO  [px4] Startup script returned successfully",
    )
)


def test_simulated_delivery_runner_creates_evidence_chain_without_dispatch(
    tmp_path: Path,
) -> None:
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": "runner-v0-fixture",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "gz-sim-runner-v0-fixture",
                "vehicle_id": "vehicle-runner-v0-fixture",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "35.681236,139.767125,16.0",
                "battery_percent": 88.0,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
                "pickup_reached": True,
                "dropoff_reached": True,
                "route_progress_percent": 100.0,
            },
        }
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    updated = create_and_run_simulated_delivery_task_v0(
        delivery_mission_contract=build_delivery_contract(),
        sanitized_telemetry=telemetry,
        title="Simulated delivery runner contract",
        owner_session_id="runner-fixture-session",
        owner_user_id="runner-fixture-user",
        now=NOW,
        task_store_factory=lambda: store,
    )
    timeline = store.query_timeline(updated["task_id"])
    artifacts = updated["artifacts"]
    result = artifacts["simulated_delivery_runner_result"]

    assert updated["status"] == "completed"
    assert updated["ended_at"] is not None
    for key in (
        "gazebo_delivery_telemetry_window",
        "hil_telemetry_evidence",
        "hil_telemetry_review",
        "delivery_mission_policy_review",
        "delivery_mission_scorecard",
        "delivery_mission_gate_result",
        "simulated_delivery_episode",
        "delivery_progress_review",
        "delivery_recovery_decision",
    ):
        assert key in artifacts
    assert any(
        event["event_type"] == "status_changed" for event in timeline["events"]
    )
    assert {"approval", "promotion_package", "reuse_plan", "runtime_reuse"}.isdisjoint(
        artifacts
    )
    for field in (
        "live_execution_allowed",
        "physical_execution_invoked",
        "command_payload_allowed",
        "dispatch_implementation_present",
        "gazebo_entity_mutation_allowed",
        "ros_dispatch_allowed",
        "mavlink_dispatch_allowed",
        "actuator_execution_allowed",
    ):
        assert result[field] is False


def test_px4_controlled_runner_uses_bounded_artifact_stubs_only(
    tmp_path: Path,
) -> None:
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    observation = build_px4_sitl_delivery_observation_from_logs(
        PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
    )
    preflight = build_px4_simulation_command_preflight_artifacts(
        profile=profile,
        observation=observation,
        operator_approval_performed=True,
        now=NOW,
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="px4_controlled_gazebo_delivery_runner_v0",
        title="PX4 controlled delivery runner contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )

    updated = run_px4_controlled_gazebo_delivery_mission_v0_task(
        task["task_id"],
        preflight_artifacts=preflight,
        now=NOW,
        task_store_factory=lambda: store,
    )
    dispatches = updated["artifacts"]["px4_simulation_mavlink_dispatch_results"]
    runner = updated["artifacts"]["px4_controlled_gazebo_delivery_runner_result"]

    assert updated["status"] == "completed"
    assert updated["artifacts"]["existing"] == {"kept": True}
    assert len(dispatches) == 4
    assert runner["observed_delivery_phases"] == [
        "pickup",
        "enroute",
        "dropoff",
        "completed",
    ]
    assert runner["final_status"] == "completed"
    for dispatch in dispatches:
        assert dispatch["dispatch_mode"] == "artifact_stub"
        assert dispatch["simulation_only"] is True
        assert dispatch["bounded_allowlist_enforced"] is True
        assert dispatch["operator_approval_performed"] is True
        assert dispatch["raw_mavlink_payload_present"] is False
        assert dispatch["mavlink_socket_opened"] is False
        assert dispatch["mavlink_frame_sent"] is False
        assert dispatch["hardware_target_allowed"] is False
    assert runner["hardware_target_allowed"] is False
    assert runner["physical_execution_invoked"] is False


def test_sitl_phase_derived_episode_remains_preflight_without_dropoff_claim(
    tmp_path: Path,
) -> None:
    log_text = "\n".join(
        (
            "PX4_SIM_MODEL: gz_x500",
            "INFO  [gz_bridge] world: default, model: x500_0",
            "INFO  [gz_bridge] Gazebo world is ready",
            "INFO  [px4] Startup script returned successfully",
        )
    )
    pose_samples = tuple({"x": 0.0, "y": 0.0, "z": -0.01} for _ in range(3))
    sitl_run, source_artifacts = build_px4_gazebo_sitl_telemetry_run(
        log_text=log_text,
        pose_samples=pose_samples,
        mavlink_frame_count=42,
        mavlink_heartbeat_count=5,
        mavlink_observation_window_seconds=5.5,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=6),
        max_duration_seconds=30.0,
        source_id="px4-gazebo-sitl-phase-derived-fixture",
    )
    contract = build_completed_delivery_contract().model_copy(
        update={
            "metadata": {
                "sitl_phase_geometry": {
                    "home_xy_m": [0.0, 0.0],
                    "dropoff_xy_m": [30.0, 0.0],
                    "takeoff_altitude_m": 1.0,
                    "staged_ascent_altitude_m": 2.0,
                    "dropoff_approach_radius_m": 3.0,
                    "summit_approach_radius_m": 8.0,
                }
            }
        }
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="control_supervisor",
        title="SITL phase-derived episode contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    artifacts = attach_simulated_delivery_episode_from_sitl_telemetry_run(
        task_id=task["task_id"],
        delivery_mission_contract=contract,
        sitl_telemetry_run=sitl_run,
        sanitized_telemetry=source_artifacts["px4_gazebo_sanitized_telemetry"],
        hil_telemetry_review=source_artifacts["hil_telemetry_review"],
        autonomy_gate_result=source_artifacts["autonomy_gate_result"],
        now=NOW,
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])
    episode = artifacts["simulated_delivery_episode"]
    trace = artifacts["delivery_replay_trace"]

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert episode["schema_version"] == SIMULATED_DELIVERY_EPISODE_SCHEMA_VERSION
    assert episode["phase"] == "preflight"
    assert episode["phase_history"] == ["preflight"]
    assert episode["final_status"] == "ready_for_simulation"
    assert episode["passed"] is True
    assert episode["dropoff_verified"] is False
    assert episode["sitl_telemetry_run_ref"] == trace["sitl_telemetry_run_ref"]
    assert episode["gazebo_execution_invoked_by_episode"] is False
    assert episode["physical_execution_invoked"] is False
    assert episode["mavlink_dispatch_allowed"] is False
    assert episode["px4_mission_upload_allowed"] is False
    assert {"approval", "promotion_package", "runtime_reuse"}.isdisjoint(
        stored["artifacts"]
    )
