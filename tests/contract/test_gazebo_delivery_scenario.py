from pathlib import Path

import pytest

from src.runtime.delivery_progress_review import (
    DELIVERY_PROGRESS_BUCKET_LANDING_ZONE_UNAVAILABLE,
    DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION,
)
from src.runtime.gazebo_delivery_scenario import (
    GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION,
    GAZEBO_DELIVERY_SCENARIO_VARIANTS,
    attach_gazebo_delivery_scenario,
    build_gazebo_delivery_scenario_variant,
)
from src.runtime.px4_gazebo_telemetry import sanitize_px4_gazebo_telemetry_sample
from src.runtime.simulated_delivery_runner import run_simulated_delivery_task_v0
from src.runtime.task_store import TaskStore
from tests.fixtures.delivery_artifact_chain import NOW, build_delivery_contract


SCENARIO_BOUNDARY_FIELDS = (
    "command_payload_allowed",
    "dispatch_implementation_present",
    "gazebo_entity_mutation_allowed",
    "live_execution_allowed",
    "physical_execution_invoked",
    "ros_dispatch_allowed",
    "mavlink_dispatch_allowed",
    "actuator_execution_allowed",
)


def test_gazebo_delivery_scenario_attach_preserves_task_and_authority_boundary(
    tmp_path: Path,
) -> None:
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="control_supervisor",
        title="Gazebo delivery scenario contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    artifacts = attach_gazebo_delivery_scenario(
        task["task_id"],
        delivery_mission_contract=build_delivery_contract(),
        world_ref="worlds/delivery_minimal.sdf",
        now=NOW,
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])
    scenario = artifacts["gazebo_delivery_scenario"]

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert scenario["schema_version"] == GAZEBO_DELIVERY_SCENARIO_SCHEMA_VERSION
    assert scenario["simulator_kind"] == "gazebo_sim"
    assert scenario["world_ref"] == "worlds/delivery_minimal.sdf"
    assert scenario["simulation_only"] is True
    assert {"approval", "promotion_package", "runtime_reuse"}.isdisjoint(
        stored["artifacts"]
    )
    for field in SCENARIO_BOUNDARY_FIELDS:
        assert scenario[field] is False


def test_scenario_variant_ids_are_deterministic_distinct_and_non_executing() -> None:
    contract = build_delivery_contract()
    first = {
        variant: build_gazebo_delivery_scenario_variant(
            delivery_mission_contract=contract,
            variant=variant,
            now=NOW,
        )
        for variant in GAZEBO_DELIVERY_SCENARIO_VARIANTS
    }
    second = {
        variant: build_gazebo_delivery_scenario_variant(
            delivery_mission_contract=contract,
            variant=variant,
            now=NOW,
        )
        for variant in GAZEBO_DELIVERY_SCENARIO_VARIANTS
    }

    assert {key: value.scenario_id for key, value in first.items()} == {
        key: value.scenario_id for key, value in second.items()
    }
    assert len({scenario.scenario_id for scenario in first.values()}) == len(first)
    for scenario in first.values():
        assert scenario.metadata["opt_in_only"] is True
        assert scenario.metadata["headless_compatible"] is True
        assert scenario.metadata["requires_gui"] is False
        assert scenario.metadata["command_control_ports_exposed"] is False
        for field in SCENARIO_BOUNDARY_FIELDS:
            assert getattr(scenario, field) is False


@pytest.mark.parametrize(
    ("variant", "route_violation", "landing_available", "status", "reason"),
    (
        ("nominal_delivery", False, True, "completed", None),
        (
            "blocked_route_geofence",
            True,
            True,
            "blocked",
            DELIVERY_PROGRESS_BUCKET_ROUTE_GEOFENCE_VIOLATION,
        ),
        (
            "landing_zone_unavailable",
            False,
            False,
            "blocked",
            DELIVERY_PROGRESS_BUCKET_LANDING_ZONE_UNAVAILABLE,
        ),
    ),
)
def test_scenario_corpus_runner_converges_without_execution_authority(
    tmp_path: Path,
    variant: str,
    route_violation: bool,
    landing_available: bool,
    status: str,
    reason: str | None,
) -> None:
    contract = build_delivery_contract()
    scenario = build_gazebo_delivery_scenario_variant(
        delivery_mission_contract=contract,
        variant=variant,
        now=NOW,
    )
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": f"scenario-corpus-{variant}",
            "source": {
                "source_kind": "gz_sim_delivery_entity_state_pose",
                "source_id": "gz-sim-scenario-corpus",
                "vehicle_id": f"vehicle-{variant}",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "25.1,0.0,0.2",
                "battery_percent": 88.0,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
                "pickup_reached": True,
                "dropoff_reached": True,
                "route_progress_percent": 100.0,
                "route_geofence_violation": route_violation,
                "landing_zone_available": landing_available,
            },
        }
    )
    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="simulated_delivery_runner",
        title=f"Gazebo delivery scenario corpus: {variant}",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    updated = run_simulated_delivery_task_v0(
        task["task_id"],
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        sanitized_telemetry=telemetry,
        now=NOW,
        task_store_factory=lambda: store,
    )
    result = updated["artifacts"]["simulated_delivery_runner_result"]

    assert updated["status"] == status
    assert updated["artifacts"]["existing"] == {"kept": True}
    if reason is None:
        assert result["blocked_reasons"] == []
    else:
        assert reason in result["blocked_reasons"]
        assert updated["artifacts"]["delivery_recovery_decision"][
            "primary_action"
        ] == "operator_escalation_required"
    for field in (
        "live_execution_allowed",
        "physical_execution_invoked",
        "command_payload_allowed",
        "ros_dispatch_allowed",
        "mavlink_dispatch_allowed",
        "actuator_execution_allowed",
    ):
        assert result[field] is False
