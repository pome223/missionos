from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_mission_gate import build_delivery_mission_gate_artifacts
from src.runtime.delivery_mission_policy_review import (
    build_delivery_mission_policy_review,
)
from src.runtime.delivery_progress_review import build_delivery_progress_review
from src.runtime.gazebo_delivery_scenario import build_gazebo_delivery_scenario
from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.simulated_delivery_episode import build_simulated_delivery_episode


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class DeliveryArtifactChain:
    contract: Any
    telemetry: Any
    hil_review: dict[str, Any]
    policy_review: Any
    gate_artifacts: dict[str, Any]
    episode: Any
    scenario: Any
    progress_review: Any


def build_delivery_contract() -> Any:
    return build_delivery_mission_contract(
        mission_id="delivery-contract-fixture-001",
        pickup_location={
            "location_id": "pickup-pad-a",
            "label": "Warehouse pad A",
            "latitude": 35.681236,
            "longitude": 139.767125,
            "altitude_m": 16.0,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "label": "Customer pad B",
            "latitude": 35.689487,
            "longitude": 139.691706,
            "altitude_m": 41.0,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={
            "package_id": "pkg-contract-fixture",
            "max_weight_kg": 1.2,
            "max_length_m": 0.3,
            "max_width_m": 0.2,
            "max_height_m": 0.15,
        },
        geofence_constraints={"allowed_regions": ["sim-delivery-corridor"]},
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
            "accepted_surface_kinds": ["marked_pad", "clear_rooftop"],
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


def build_delivery_artifact_chain(
    *,
    battery_percent: float = 30.0,
    pickup_reached: bool = True,
    route_progress_percent: float = 42.5,
) -> DeliveryArtifactChain:
    contract = build_delivery_contract()
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": "delivery-artifact-chain-fixture",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "gz-sim-delivery-world",
                "vehicle_id": "vehicle-delivery-contract-fixture",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "35.681236,139.767125,16.0",
                "battery_percent": battery_percent,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
                "pickup_reached": pickup_reached,
                "dropoff_reached": False,
                "route_progress_percent": route_progress_percent,
            },
        }
    )
    hil_review = build_px4_gazebo_hil_review_gate_smoke(
        telemetry,
        freshness_threshold_seconds=10.0,
        now=NOW,
    )["hil_telemetry_review"]
    policy_review = build_delivery_mission_policy_review(
        delivery_mission_contract=contract,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_review,
        now=NOW,
    )
    gate_artifacts = build_delivery_mission_gate_artifacts(
        delivery_mission_contract=contract,
        delivery_mission_policy_review=policy_review,
        now=NOW,
    )
    episode = build_simulated_delivery_episode(
        delivery_mission_contract=contract,
        delivery_mission_policy_review=policy_review,
        delivery_mission_scorecard=gate_artifacts["delivery_mission_scorecard"],
        delivery_mission_gate_result=gate_artifacts["delivery_mission_gate_result"],
        now=NOW,
    )
    scenario = build_gazebo_delivery_scenario(
        delivery_mission_contract=contract,
        now=NOW,
    )
    progress_review = build_delivery_progress_review(
        delivery_mission_contract=contract,
        gazebo_delivery_scenario=scenario,
        simulated_delivery_episode=episode,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_review,
        now=NOW,
    )
    return DeliveryArtifactChain(
        contract=contract,
        telemetry=telemetry,
        hil_review=hil_review,
        policy_review=policy_review,
        gate_artifacts=gate_artifacts,
        episode=episode,
        scenario=scenario,
        progress_review=progress_review,
    )
