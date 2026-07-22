"""SITL evidence contracts migrated from standalone in-process wrappers."""

from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.px4_gazebo_bounded_simulation_runner import (
    build_px4_gazebo_bounded_simulation_run,
)
from src.runtime.px4_gazebo_delivery_world_profile import (
    build_px4_gazebo_delivery_world_profile,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    approve_px4_gazebo_mission_scenario_for_bounded_simulation,
    run_px4_gazebo_mission_scenario_designer,
)
from src.runtime.px4_gazebo_sitl_dropoff_verification import (
    PX4_GAZEBO_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION,
    attach_px4_gazebo_sitl_dropoff_verification,
    build_px4_gazebo_sitl_dropoff_flight_fact,
    build_px4_gazebo_sitl_dropoff_verification,
    build_px4_gazebo_sitl_payload_release_event,
    dropoff_evidence_from_sitl_verification,
)
from src.runtime.px4_gazebo_state_correlation import (
    DEFAULT_GAZEBO_DELIVERY_ENTITY_NAME,
    attach_px4_gazebo_delivery_state_readiness_artifacts,
)
from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.px4_sitl_delivery_observation import (
    build_px4_sitl_delivery_observation_from_logs,
)
from src.runtime.simulated_delivery_episode import (
    build_simulated_delivery_episode_from_bounded_gazebo_run,
)
from src.runtime.task_store import TaskStore


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
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


def _delivery_contract():
    return build_delivery_mission_contract(
        mission_id="sitl-dropoff-verification-contract",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "latitude": 35.689487,
            "longitude": 139.691706,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-sitl-dropoff", "max_weight_kg": 1.2},
        weather_constraints={
            "max_wind_speed_mps": 6.0,
            "max_precipitation_mm_per_hour": 0.0,
            "min_visibility_m": 1500.0,
        },
        battery_policy={
            "minimum_takeoff_percent": 80,
            "return_to_home_percent": 35,
            "reserve_landing_percent": 25,
        },
        landing_zone_policy={
            "min_clear_radius_m": 3.0,
            "max_slope_degrees": 5.0,
            "accepted_surface_kinds": ["marked_pad"],
        },
        telemetry_requirements={
            "required_measurements": [
                "position",
                "battery_percent",
                "vehicle_health",
                "weather_snapshot",
            ],
            "max_freshness_seconds": 2.0,
        },
        now=NOW,
    )


def _bounded_artifacts():
    designed = run_px4_gazebo_mission_scenario_designer(
        prompt="低高度の配送地点に1kgの荷物を届ける",
        now=NOW,
    )
    approved = approve_px4_gazebo_mission_scenario_for_bounded_simulation(
        proposal=designed["scenario_proposal"],
        validation=designed["validation_result"],
        now=NOW,
    )
    request = approved["bounded_simulation_request"]
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": "sitl-dropoff-verification-contract",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "sitl-dropoff-verification-contract",
                "vehicle_id": "x500_0",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "0.14,0.10,0.08",
                "battery_percent": 88.0,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
                "landing_zone_available": True,
            },
        }
    )
    hil_gate = build_px4_gazebo_hil_review_gate_smoke(
        telemetry,
        freshness_threshold_seconds=60.0,
        now=NOW,
    )
    run = build_px4_gazebo_bounded_simulation_run(
        request=request,
        started_at=NOW,
        finished_at=NOW,
        max_duration_seconds=300,
        max_log_lines=260,
        observed_log_line_count=34,
        telemetry_captured_at=NOW,
        max_telemetry_age_seconds=300,
        telemetry_age_seconds=0.0,
        telemetry_refs=(f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}",),
        gate_ref=(
            "autonomy_gate_result:"
            f"{hil_gate['autonomy_gate_result']['gate_id']}"
        ),
        hil_review_ref=(
            "hil_telemetry_review:"
            f"{hil_gate['hil_telemetry_review']['review_id']}"
        ),
        provenance={
            "world_name": "empty",
            "world_ref": "/tmp/empty.sdf",
            "world_sdf_path": "/tmp/empty.sdf",
            "network_mode": "none",
            "read_only_rootfs": True,
            "privileged": False,
            "cap_drop": ["ALL"],
        },
    )
    return request, telemetry, hil_gate, run


def test_dropoff_flight_fact_verifies_without_granting_execution() -> None:
    contract = _delivery_contract()
    request, telemetry, hil_gate, run = _bounded_artifacts()
    release = build_px4_gazebo_sitl_payload_release_event(
        event_source="gazebo_gripper_detach_event",
        payload_id="pkg-sitl-dropoff",
        release_position_x_m=0.15,
        release_position_y_m=0.12,
        release_position_z_m=0.08,
        observed_at=NOW,
    )
    fact = build_px4_gazebo_sitl_dropoff_flight_fact(
        vehicle_id="x500_0",
        dropoff_zone_id="dropoff-pad-b",
        position_x_m=0.14,
        position_y_m=0.1,
        position_z_m=0.08,
        dropoff_target_x_m=0.0,
        dropoff_target_y_m=0.0,
        dropoff_target_altitude_m=0.0,
        mission_item_reached_observed=True,
        mission_item_reached_seq=2,
        mission_item_reached_at=NOW,
        payload_release_event=release,
        telemetry_ref="px4_gazebo_sitl_telemetry_sample:dropoff-contract",
        sitl_mission_upload_receipt_ref=(
            "px4_gazebo_sitl_mission_upload_receipt:contract"
        ),
        observed_at=NOW,
    )
    verification = build_px4_gazebo_sitl_dropoff_verification(
        delivery_mission_contract=contract,
        dropoff_flight_fact=fact,
        payload_release_event=release,
        now=NOW,
    )
    episode = build_simulated_delivery_episode_from_bounded_gazebo_run(
        delivery_mission_contract=contract,
        bounded_simulation_request=request,
        bounded_simulation_run=run,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=hil_gate["autonomy_gate_result"],
        dropoff_evidence=dropoff_evidence_from_sitl_verification(verification),
        now=NOW,
    )["simulated_delivery_episode"]

    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="SITL dropoff verification contract",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        attach_px4_gazebo_sitl_dropoff_verification(
            task_id=task["task_id"],
            delivery_mission_contract=contract,
            dropoff_flight_fact=fact,
            payload_release_event=release,
            now=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])

    assert verification.schema_version == PX4_GAZEBO_SITL_DROPOFF_VERIFICATION_SCHEMA_VERSION
    assert verification.status.value == "verified"
    assert verification.dropoff_verified is True
    assert verification.payload_release_observed is True
    assert verification.release_position_within_dropoff_zone is True
    assert verification.release_altitude_within_tolerance is True
    assert verification.release_within_mission_item_time_window is True
    assert episode.dropoff_verified is True
    assert "dropoff_verified" in {phase.value for phase in episode.phase_history}
    assert verification.physical_execution_invoked is False
    assert verification.hardware_target_allowed is False
    assert verification.gazebo_entity_mutation_performed is False
    assert stored is not None and stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}


def test_state_correlation_readiness_is_read_only_and_preserves_task() -> None:
    profile = build_px4_gazebo_delivery_world_profile(now=NOW)
    observation = build_px4_sitl_delivery_observation_from_logs(
        PX4_SITL_LOGS,
        captured_at=NOW,
        profile=profile,
    )
    with TemporaryDirectory() as tmp:
        store = TaskStore(f"{tmp}/tasks.db")
        task = store.create(
            kind="px4_gazebo_delivery_state_readiness",
            title="state correlation contract",
            status="running",
        )
        artifacts = attach_px4_gazebo_delivery_state_readiness_artifacts(
            task["task_id"],
            profile=profile,
            observation=observation,
            gazebo_pose={
                "entity_name": DEFAULT_GAZEBO_DELIVERY_ENTITY_NAME,
                "x": -10.0,
                "y": 0.0,
                "z": 0.05,
            },
            checked_at=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])

    correlation = artifacts["px4_gazebo_delivery_state_correlation"]
    readiness = artifacts["px4_sitl_delivery_readiness_diagnostics"]
    assert stored is not None and stored["status"] == "running"
    assert correlation["state_correlation_status"] == "ready"
    assert readiness["readiness_status"] == "ready"
    assert correlation["mavlink_dispatch_allowed"] is False
    assert readiness["ros_dispatch_allowed"] is False
    assert readiness["hardware_target_allowed"] is False
    assert readiness["physical_execution_invoked"] is False
