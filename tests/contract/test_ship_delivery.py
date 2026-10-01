from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import dist

import pytest
from missionos_core import canonical_sha256
from pydantic import ValidationError

from src.runtime import ship_delivery
from src.runtime.ship_delivery import (
    ShipDeliveryScenario,
    build_ship_delivery_contract,
    run_ship_delivery_fixture,
    verify_ship_delivery_receipts,
)


_NO_LIVE_EXECUTION_FIELDS = (
    "physical_execution_invoked",
    "px4_runtime_invoked",
    "vla_invoked",
    "wam_invoked",
)


def _rehash(receipt: dict) -> None:
    receipt["sha256"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "sha256"}
    )


def _path_distance(result: dict) -> float:
    positions = [sample["position_enu_m"] for sample in result["trajectory"]]
    return sum(dist(first, second) for first, second in zip(positions, positions[1:]))


def test_default_scenario_is_a_single_one_kilometre_offshore_round_trip() -> None:
    scenario = ShipDeliveryScenario()

    assert scenario.offshore_distance_m == 1000.0
    result = run_ship_delivery_fixture(scenario, operator_approved=True)

    assert result["status"] == "completed"
    assert result["fixture_mission_completed"] is True
    assert result["delivery_verified"] is True
    assert result["recovery_verified"] is True
    assert result["identity_continuity_verified"] is True
    assert result["stage_receipts"]
    assert result["trajectory"]
    assert result["trajectory"][-1]["position_enu_m"] == [0, 0, 0]
    assert result["remaining_battery_wh"] >= scenario.reserve_wh
    assert _path_distance(result) >= 2 * (
        scenario.offshore_distance_m + scenario.urban_distance_m
    )
    assert all(result[field] is False for field in _NO_LIVE_EXECUTION_FIELDS)
    # The parent coordinator checks stage lineage, not physical mission closure.
    assert result["coordinator"]["mission_completion_claimed"] is False
    verified = verify_ship_delivery_receipts(scenario, result["stage_receipts"])
    assert verified["verified"] is True
    assert verified["reasons"] == []


def test_missing_operator_approval_prevents_every_fixture_stage() -> None:
    result = run_ship_delivery_fixture(
        ShipDeliveryScenario(), operator_approved=False
    )

    assert result["status"] == "blocked"
    assert result["fixture_mission_completed"] is False
    assert result["delivery_verified"] is False
    assert result["recovery_verified"] is False
    assert result["stage_receipts"] == []
    assert all(result[field] is False for field in _NO_LIVE_EXECUTION_FIELDS)


def test_approval_defaults_to_unapproved() -> None:
    result = run_ship_delivery_fixture(ShipDeliveryScenario())

    assert result["status"] == "blocked"
    assert result["stage_receipts"] == []


def test_contract_is_frozen_to_the_scenario() -> None:
    scenario = ShipDeliveryScenario()
    contract = build_ship_delivery_contract(scenario)

    assert contract.parent_mission_sha256 == build_ship_delivery_contract(
        scenario
    ).parent_mission_sha256
    assert contract.parent_mission_sha256 != build_ship_delivery_contract(
        ShipDeliveryScenario(wind_mps=5)
    ).parent_mission_sha256
    assert contract.identity_continuity_claimed is False
    assert contract.shared_world_claimed is False


@pytest.mark.parametrize(
    "scenario",
    [
        ShipDeliveryScenario(urban_perception_ready=False),
        ShipDeliveryScenario(battery_wh=45, reserve_wh=40),
    ],
    ids=["urban_perception_unavailable", "insufficient_round_trip_reserve"],
)
def test_failed_safety_gate_cannot_report_delivery_or_recovery(
    scenario: ShipDeliveryScenario,
) -> None:
    result = run_ship_delivery_fixture(scenario, operator_approved=True)

    assert result["status"] == "blocked"
    assert result["fixture_mission_completed"] is False
    assert result["delivery_verified"] is False
    assert result["recovery_verified"] is False
    assert all(result[field] is False for field in _NO_LIVE_EXECUTION_FIELDS)
    assert result["stage_receipts"] == []
    assert result["trajectory"] == []


def test_wait_and_detour_change_the_executed_path_and_time() -> None:
    clear = run_ship_delivery_fixture(ShipDeliveryScenario(), operator_approved=True)
    wait_scenario = ShipDeliveryScenario(urban_blockage_s=10)
    wait = run_ship_delivery_fixture(wait_scenario, operator_approved=True)
    detour_scenario = ShipDeliveryScenario(urban_blockage_s=60)
    detour = run_ship_delivery_fixture(detour_scenario, operator_approved=True)

    assert clear["fixture_mission_completed"] is True
    assert wait["fixture_mission_completed"] is True
    assert detour["fixture_mission_completed"] is True
    assert clear["decisions"][0]["choice"] == "proceed"
    assert wait["decisions"][0]["choice"] == "wait"
    assert detour["decisions"][0]["choice"] == "detour"
    assert wait["elapsed_s"] - clear["elapsed_s"] == pytest.approx(10)
    assert _path_distance(wait) == pytest.approx(_path_distance(clear))
    assert _path_distance(detour) > _path_distance(clear)
    assert detour["elapsed_s"] > clear["elapsed_s"]
    assert wait["remaining_battery_wh"] < clear["remaining_battery_wh"]
    assert detour["remaining_battery_wh"] < clear["remaining_battery_wh"]
    assert all(sample["position_enu_m"][1] == 0 for sample in wait["trajectory"])
    assert max(sample["position_enu_m"][1] for sample in detour["trajectory"]) == (
        detour_scenario.urban_detour_offset_m
    )


@pytest.mark.parametrize("identity_field", ["vehicle_id", "payload_id", "world_id", "run_id"])
def test_identity_tamper_is_rejected_even_with_a_recomputed_receipt_hash(
    identity_field: str,
) -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    receipts[-1]["samples"][-1][identity_field] = "different-identity"
    _rehash(receipts[-1])

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert verification["identity_continuity_verified"] is False
    assert "identity_mismatch" in verification["reasons"]


@pytest.mark.parametrize("field,value,reason", [
    ("payload_attached", True, "delivery_not_observed"),
    ("payload_position_enu_m", [0, 0, 0], "payload_not_at_delivery_site"),
])
def test_release_requires_observed_payload_at_delivery_site(
    field: str, value: object, reason: str,
) -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    delivery = next(receipt for receipt in receipts if receipt["stage"] == "deliver")
    delivery["samples"][-1][field] = value
    _rehash(delivery)

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert verification["delivery_verified"] is False
    assert reason in verification["reasons"]


@pytest.mark.parametrize("field,value", [("landed", False), ("armed", True)])
def test_recovery_requires_landed_and_disarmed_observation(
    field: str, value: bool,
) -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    receipts[-1]["samples"][-1][field] = value
    _rehash(receipts[-1])

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert verification["recovery_verified"] is False
    assert "stable_recovery_not_observed" in verification["reasons"]


def test_recovery_requires_the_full_post_landing_observation_period() -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    recovery = receipts[-1]
    prior, last = recovery["samples"][-2:]
    last["time_s"] = prior["time_s"] + 1
    last["battery_wh"] = prior["battery_wh"] - scenario.power_w / 3600
    _rehash(recovery)

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert verification["recovery_verified"] is False
    assert "stable_recovery_not_observed" in verification["reasons"]


@pytest.mark.parametrize("overrides,reason", [
    ({"wind_mps": 11}, "wind_limit_exceeded"),
    ({"wind_mps": 12, "max_wind_mps": 20}, "return_groundspeed_unavailable"),
])
def test_wind_limits_prevent_launch(overrides: dict, reason: str) -> None:
    result = run_ship_delivery_fixture(
        ShipDeliveryScenario(**overrides), operator_approved=True
    )

    assert result["status"] == "blocked"
    assert reason in result["blocking_reasons"]
    assert result["stage_receipts"] == []
    assert result["trajectory"] == []


def test_stage_evidence_failure_halts_before_payload_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute_stage = ship_delivery._execute_stage
    calls = []

    def corrupted_stage(world: object, stage: str) -> dict:
        calls.append(stage)
        receipt = execute_stage(world, stage)
        if stage == "urban_entry":
            receipt["samples"][-1]["vehicle_id"] = "unrelated-drone"
            _rehash(receipt)
        return receipt

    monkeypatch.setattr(ship_delivery, "_execute_stage", corrupted_stage)
    result = run_ship_delivery_fixture(ShipDeliveryScenario(), operator_approved=True)

    assert result["status"] == "blocked"
    assert result["fixture_mission_completed"] is False
    assert result["delivery_verified"] is False
    assert result["recovery_verified"] is False
    assert calls == ["load", "launch", "sea_outbound", "urban_entry"]
    assert result["stage_receipts"][-1]["stage"] == "urban_entry"
    assert all(sample["payload_attached"] for sample in result["trajectory"])


@pytest.mark.parametrize("overrides,reason", [
    ({"urban_perception_ready": False}, "urban_perception_not_ready"),
    ({"max_wind_mps": 3}, "wind_limit_exceeded"),
])
def test_independent_verifier_enforces_scenario_gates(
    overrides: dict, reason: str,
) -> None:
    original = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(original, operator_approved=True)
    scenario = ShipDeliveryScenario(**overrides)
    receipts = deepcopy(result["stage_receipts"])
    for receipt in receipts:
        receipt["scenario_sha256"] = scenario.sha256
        for sample in receipt["samples"]:
            sample["scenario_sha256"] = scenario.sha256
        _rehash(receipt)

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert reason in verification["reasons"]


def test_return_cannot_claim_tailwind_speed_against_the_declared_headwind() -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    sea_return = next(receipt for receipt in receipts if receipt["stage"] == "sea_return")
    start = sea_return["samples"][0]["time_s"]
    end = sea_return["samples"][-1]["time_s"]
    removed_time = (end - start) / 2
    # Keep identities, receipt continuity and energy accounting coherent while
    # falsely claiming the return leg took half its observed headwind time.
    for receipt in receipts:
        for sample in receipt["samples"]:
            time = sample["time_s"]
            if start < time <= end:
                time = start + (time - start) / 2
            elif time > end:
                time -= removed_time
            sample["time_s"] = time
            sample["battery_wh"] = scenario.battery_wh - scenario.power_w * time / 3600
        _rehash(receipt)

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert verification["recovery_verified"] is False


@pytest.mark.parametrize(
    "field",
    [
        "offshore_distance_m",
        "urban_distance_m",
        "cruise_altitude_m",
        "wind_mps",
        "airspeed_mps",
        "battery_wh",
        "reserve_wh",
        "urban_blockage_s",
        "max_wait_s",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_scenario_rejects_nonfinite_physical_values(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        ShipDeliveryScenario(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "offshore_distance_m",
        "urban_distance_m",
        "cruise_altitude_m",
        "wind_mps",
        "airspeed_mps",
        "battery_wh",
        "reserve_wh",
        "urban_blockage_s",
        "max_wait_s",
    ],
)
def test_scenario_rejects_negative_physical_values(field: str) -> None:
    with pytest.raises(ValidationError):
        ShipDeliveryScenario(**{field: -1.0})


def test_incomplete_receipts_cannot_establish_delivery_and_recovery() -> None:
    scenario = ShipDeliveryScenario()
    complete = run_ship_delivery_fixture(scenario, operator_approved=True)

    for receipts in ([], complete["stage_receipts"][:-1]):
        verification = verify_ship_delivery_receipts(scenario, receipts)
        assert verification["verified"] is False
        assert verification["reasons"]


def test_receipts_cannot_be_reused_for_a_different_scenario() -> None:
    original = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(original, operator_approved=True)
    different = ShipDeliveryScenario(offshore_distance_m=1100)

    verification = verify_ship_delivery_receipts(
        different, deepcopy(result["stage_receipts"])
    )

    assert verification["verified"] is False
    assert verification["reasons"]


@pytest.mark.parametrize("mutation", ["reverse", "duplicate"])
def test_receipt_order_and_uniqueness_are_part_of_mission_verification(
    mutation: str,
) -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    if mutation == "reverse":
        receipts.reverse()
    else:
        receipts.insert(1, deepcopy(receipts[0]))

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert verification["reasons"]


def test_airborne_samples_cannot_claim_landed_and_disarmed_state() -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    for receipt in receipts:
        for sample in receipt["samples"]:
            sample["armed"] = False
            sample["landed"] = True
        _rehash(receipt)

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert "airborne_state_invalid" in verification["reasons"]


def test_malformed_urban_decision_returns_a_denial_without_raising() -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    urban_entry = next(receipt for receipt in receipts if receipt["stage"] == "urban_entry")
    urban_entry["decision"] = "proceed"
    _rehash(urban_entry)

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert "urban_decision_mismatch" in verification["reasons"]


@pytest.mark.parametrize("age_s", [31, -1], ids=["stale", "future"])
def test_receipt_wall_clock_rejects_stale_and_future_evidence(age_s: int) -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    evaluation_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    observed_at = (evaluation_time - timedelta(seconds=age_s)).isoformat()
    for receipt in receipts:
        receipt["observed_at"] = observed_at
        _rehash(receipt)

    verification = verify_ship_delivery_receipts(
        scenario, receipts, evaluated_at=evaluation_time
    )

    assert verification["verified"] is False
    assert "receipt_stale_or_future" in verification["reasons"]
    assert verification["evaluated_at"] == evaluation_time.isoformat()


def test_historical_receipts_can_be_replayed_at_their_recorded_evaluation_time() -> None:
    scenario = ShipDeliveryScenario()
    result = run_ship_delivery_fixture(scenario, operator_approved=True)
    receipts = deepcopy(result["stage_receipts"])
    historical_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for receipt in receipts:
        receipt["observed_at"] = (historical_time - timedelta(seconds=5)).isoformat()
        _rehash(receipt)

    replayed = verify_ship_delivery_receipts(
        scenario, receipts, evaluated_at=historical_time
    )

    assert replayed["verified"] is True
    assert replayed["evaluated_at"] == historical_time.isoformat()


def test_segment_crossing_an_active_obstacle_is_not_hidden_by_sparse_samples() -> None:
    scenario = ShipDeliveryScenario(urban_blockage_s=120)
    world = ship_delivery._FixtureWorld(scenario)
    receipts = []
    for stage in ship_delivery.STAGES:
        if stage != "urban_entry":
            receipts.append(ship_delivery._execute_stage(world, stage))
            continue
        world.urban_started_at = world.time_s
        samples = [world.observe()]
        coast = scenario.offshore_distance_m
        altitude = scenario.cruise_altitude_m
        destination = coast + scenario.urban_distance_m
        # Satisfy the observed detour-offset check, then return to the coast.
        world.move([coast, scenario.urban_detour_offset_m, altitude], samples)
        world.move([coast, 0, altitude], samples)
        # Both endpoints lie outside the obstacle, but the connecting segment
        # crosses its full width while the declared blockage remains active.
        samples.append(world.advance(
            [destination, 0, altitude],
            scenario.urban_distance_m / scenario.airspeed_mps,
        ))
        receipt = {
            "stage": stage,
            "samples": samples,
            "decision": {
                "choice": "detour",
                "source": "deterministic_fixture_policy",
                "blockage_duration_s": scenario.urban_blockage_s,
                "prediction_source": "scripted_fixture_schedule",
                "vla_invoked": False,
                "wam_invoked": False,
            },
            "scenario_sha256": scenario.sha256,
            "schema_version": "ship_fixture_stage.v1",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        _rehash(receipt)
        receipts.append(receipt)

    verification = verify_ship_delivery_receipts(scenario, receipts)

    assert verification["verified"] is False
    assert verification["reasons"] == ["urban_obstacle_contact"]
