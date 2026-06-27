"""Runtime smoke for bounded Gazebo delivery episode scorecard/review."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from tempfile import TemporaryDirectory

from src.runtime.delivery_episode_review import (
    DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION,
    DELIVERY_REVIEW_BUCKET_DELIVERY_COMPLETED,
    DELIVERY_REVIEW_BUCKET_HIGH_ALTITUDE_RISK,
    DELIVERY_REVIEW_BUCKET_PAYLOAD_MARGIN_RISK,
    DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_REQUIRED,
    DELIVERY_SCORECARD_SCHEMA_VERSION,
    attach_delivery_episode_scorecard_review,
)
from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.px4_gazebo_bounded_simulation_runner import (
    run_px4_gazebo_bounded_simulation_request,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    approve_px4_gazebo_mission_scenario_for_bounded_simulation,
    run_px4_gazebo_mission_scenario_designer,
)
from src.runtime.simulated_delivery_episode import (
    attach_simulated_delivery_episode_from_bounded_gazebo_run,
)
from src.runtime.task_store import TaskStore

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
VALID_GZ_SIM_LOGS = "\n".join(
    (
        "[Msg] Gazebo Sim Server v8.11.0",
        "[Msg] Loading SDF world file[/tmp/empty.sdf].",
        "[Msg] Loaded level [default]",
        "[Msg] World [empty] initialized.",
    )
)


def _contract():
    return build_delivery_mission_contract(
        mission_id="delivery-episode-scorecard-review-smoke",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "mountain-summit-pad",
            "latitude": 35.700001,
            "longitude": 139.700001,
            "altitude_m": 3000.0,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-water-5kg", "max_weight_kg": 5.0},
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


def _bounded_request() -> dict:
    designed = run_px4_gazebo_mission_scenario_designer(
        prompt="標高3000mの山頂に5kgの水を届ける",
        now=NOW,
    )
    approved = approve_px4_gazebo_mission_scenario_for_bounded_simulation(
        proposal=designed["scenario_proposal"],
        validation=designed["validation_result"],
        now=NOW,
    )
    return approved["bounded_simulation_request"]


def main() -> int:
    with TemporaryDirectory() as tmpdir:
        store = TaskStore(f"{tmpdir}/tasks.db")
        task = store.create(
            kind="control_supervisor",
            title="Delivery episode scorecard/review smoke",
            status="running",
            artifacts={"existing": {"kept": True}},
        )
        request = _bounded_request()
        run_artifacts = run_px4_gazebo_bounded_simulation_request(
            task_id=task["task_id"],
            request=request,
            log_text=VALID_GZ_SIM_LOGS,
            started_at=NOW,
            finished_at=NOW,
            max_duration_seconds=30,
            max_log_lines=10,
            provenance={
                "source_image": "ghcr.io/openrobotics/gazebo:harmonic-full",
                "world_name": "empty",
                "world_ref": "/tmp/empty.sdf",
                "world_sdf_path": "/tmp/empty.sdf",
                "container_exit_code": 0,
                "network_mode": "none",
                "read_only_rootfs": True,
                "privileged": False,
                "cap_drop": ["ALL"],
                "port_bindings": {},
            },
            task_store_factory=lambda: store,
        )
        episode_artifacts = attach_simulated_delivery_episode_from_bounded_gazebo_run(
            task["task_id"],
            delivery_mission_contract=_contract(),
            bounded_simulation_request=request,
            bounded_simulation_run=run_artifacts["px4_gazebo_bounded_simulation_run"],
            sanitized_telemetry=run_artifacts["px4_gazebo_sanitized_telemetry"],
            hil_telemetry_review=run_artifacts["hil_telemetry_review"],
            autonomy_gate_result=run_artifacts["autonomy_gate_result"],
            dropoff_evidence={
                "evidence_ref": "simulated_dropoff_evidence:mountain-summit-pad",
                "dropoff_verified": True,
                "landing_error_m": 0.32,
            },
            now=NOW,
            task_store_factory=lambda: store,
        )
        review_artifacts = attach_delivery_episode_scorecard_review(
            task["task_id"],
            delivery_mission_contract=_contract(),
            simulated_delivery_episode=episode_artifacts["simulated_delivery_episode"],
            delivery_replay_trace=episode_artifacts["delivery_replay_trace"],
            hil_telemetry_review=run_artifacts["hil_telemetry_review"],
            autonomy_gate_result=run_artifacts["autonomy_gate_result"],
            sanitized_telemetry=run_artifacts["px4_gazebo_sanitized_telemetry"],
            now=NOW,
            task_store_factory=lambda: store,
        )
        stored = store.get(task["task_id"])

    assert stored is not None
    scorecard = review_artifacts["delivery_scorecard"]
    review = review_artifacts["delivery_episode_review"]
    summary = {
        "scorecard_schema_version": scorecard["schema_version"],
        "review_schema_version": review["schema_version"],
        "delivery_completed": scorecard["delivery_completed"],
        "dropoff_completed": scorecard["dropoff_completed"],
        "staged_ascent_completed": scorecard["staged_ascent_completed"],
        "battery_reserve_ok": scorecard["battery_reserve_ok"],
        "telemetry_freshness_ok": scorecard["telemetry_freshness_ok"],
        "gate_passed": scorecard["gate_passed"],
        "review_status": review["status"],
        "review_passed": review["passed"],
        "review_buckets": review["buckets"],
        "warning_buckets": review["warning_buckets"],
        "task_status_preserved": stored["status"] == "running",
        "existing_artifact_retained": "existing" in stored["artifacts"],
        "scorecard_attached": "delivery_scorecard" in stored["artifacts"],
        "review_attached": "delivery_episode_review" in stored["artifacts"],
        "approval_promotion_reuse_created": any(
            key in stored["artifacts"]
            for key in ("approval", "promotion_package", "runtime_reuse")
        ),
        "physical_execution_invoked": scorecard["physical_execution_invoked"],
        "hardware_target_allowed": scorecard["hardware_target_allowed"],
        "mavlink_dispatch_allowed": scorecard["mavlink_dispatch_allowed"],
        "ros_dispatch_allowed": scorecard["ros_dispatch_allowed"],
        "actuator_execution_allowed": scorecard["actuator_execution_allowed"],
        "approval_free_stronger_execution_allowed": scorecard[
            "approval_free_stronger_execution_allowed"
        ],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))

    assert summary["scorecard_schema_version"] == DELIVERY_SCORECARD_SCHEMA_VERSION
    assert summary["review_schema_version"] == DELIVERY_EPISODE_REVIEW_SCHEMA_VERSION
    assert summary["delivery_completed"] is True
    assert summary["dropoff_completed"] is True
    assert summary["staged_ascent_completed"] is True
    assert summary["review_status"] == "passed"
    assert summary["review_passed"] is True
    assert DELIVERY_REVIEW_BUCKET_DELIVERY_COMPLETED in summary["review_buckets"]
    assert DELIVERY_REVIEW_BUCKET_STAGED_ASCENT_REQUIRED in summary["warning_buckets"]
    assert DELIVERY_REVIEW_BUCKET_HIGH_ALTITUDE_RISK in summary["warning_buckets"]
    assert DELIVERY_REVIEW_BUCKET_PAYLOAD_MARGIN_RISK in summary["warning_buckets"]
    assert summary["task_status_preserved"] is True
    assert summary["existing_artifact_retained"] is True
    assert summary["scorecard_attached"] is True
    assert summary["review_attached"] is True
    assert summary["approval_promotion_reuse_created"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["mavlink_dispatch_allowed"] is False
    assert summary["ros_dispatch_allowed"] is False
    assert summary["actuator_execution_allowed"] is False
    assert summary["approval_free_stronger_execution_allowed"] is False
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
