"""Negative evidence and execution-boundary checks; no simulator started here."""

from copy import deepcopy

import pytest

from src.runtime.ship_delivery import ShipDeliveryScenario
from src.runtime.ship_delivery_sitl import (
    _read_jsonl,
    _return_permit,
    build_ship_sitl_missions,
    run_ship_delivery_sitl,
    verify_ship_sitl_delivery,
    verify_ship_sitl_run,
)


def evidence():
    config = {
        "run_id": "run",
        "world_sha256": "world",
        "plan_sha256": "plan",
        "goal_north_m": 1200,
        "reserve_fraction": 0.18,
        "wind_mps": 4,
    }

    def row(t, phase, xyz, payload, *, landed=False, contact=False):
        return {
            "run_id": "run",
            "world_sha256": "world",
            "elapsed_s": t,
            "phase": phase,
            "vehicle": {"id": 55, "xyz": xyz},
            "payload": {"id": 49, "xyz": payload},
            "ship": {"id": 11, "xyz": [0, 0, -1]},
            "poses_fresh": True,
            "payload_contact": contact,
            "deck_contact": landed,
            "landed": landed,
            "arming_state": 1 if landed else 2,
            "velocity_ned": [0, 0, 0],
            "battery_remaining": 0.8,
        }

    samples = [
        row(1, "outbound", [0, 300, 30], [0, 300, 30.04]),
        *[
            row(t, "delivery_verify", [0, 1200, 3], [0, 1200, 0.09], contact=True)
            for t in range(15, 19)
        ],
        row(20, "return", [0, 1200, 30], [0, 1200, 0.09], contact=True),
        *[
            row(t, "return", [0, 0, 0.2], [0, 1200, 0.09], landed=True, contact=True)
            for t in range(40, 44)
        ],
    ]
    events = [
        {"event": n, "run_id": "run", "receipt": {"mission_ack_type": 0}}
        for n in (
            "outbound_upload",
            "outbound_ready",
            "outbound_requested",
            "wind_observed",
            "detach_published",
            "return_authorized",
            "return_hold_observed",
            "return_upload",
            "return_ready",
            "return_requested",
            "return_climb_observed",
            "recovery_candidate_observed",
        )
    ]
    for e in events:
        if e["event"] == "return_authorized":
            e["binding"] = _return_permit(config, samples[:5])
        if e["event"] == "wind_observed":
            e["readback"] = "linear_velocity {\n y: 4\n}\nenable_wind: true"
    return config, samples, events


def test_two_missions_hold_forever_until_verified_delivery():
    missions = build_ship_sitl_missions(ShipDeliveryScenario())
    assert missions["goal_north_m"] == 1200
    assert missions["outbound"][0]["command"] == 22
    assert missions["outbound"][-1]["command"] == 17
    assert missions["outbound"][-1]["altitude_m"] == 3
    assert missions["return"][-1]["command"] == 21
    assert missions["return"][-1]["latitude_deg"] == 35.3195
    for leg in ("outbound", "return"):
        assert [i["seq"] for i in missions[leg]] == list(range(len(missions[leg])))


def test_missing_approval_starts_no_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.runtime.ship_delivery_sitl._run", lambda *a, **k: pytest.fail("Docker invoked")
    )
    with pytest.raises(PermissionError):
        run_ship_delivery_sitl(ShipDeliveryScenario(), output_dir=tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_sitl_refuses_unsupported_dynamic_scenario_before_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.runtime.ship_delivery_sitl._run", lambda *a, **k: pytest.fail("Docker invoked")
    )
    with pytest.raises(ValueError, match="static urban corridor"):
        run_ship_delivery_sitl(
            ShipDeliveryScenario(urban_blockage_s=10),
            output_dir=tmp_path / "run",
            operator_approved=True,
        )


def test_real_observation_contract_satisfied_by_explicit_test_evidence():
    config, samples, events = evidence()
    assert verify_ship_sitl_delivery(samples[:5], config)["verified"]
    assert verify_ship_sitl_run(samples, events, config)["verified"]


@pytest.mark.parametrize(
    "mutation", ["contact", "attached", "identity", "world", "stability", "stale", "gap"]
)
def test_delivery_requires_observed_touchdown_not_detach_ack(mutation):
    config, samples, _ = evidence()
    rows = deepcopy(samples[:5])
    if mutation == "contact":
        rows[-1]["payload_contact"] = False
    elif mutation == "attached":
        rows[-1]["payload"]["xyz"] = [0, 1200, 3]
    elif mutation == "identity":
        rows[-1]["payload"]["id"] = 999
    elif mutation == "world":
        rows[-1]["world_sha256"] = "other"
    elif mutation == "stability":
        rows[-1]["elapsed_s"] = 16
    elif mutation == "stale":
        rows[-2]["poses_fresh"] = False
    else:
        rows[-1]["elapsed_s"] = 22
    assert not verify_ship_sitl_delivery(rows, config)["verified"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("deck_contact", False),
        ("landed", False),
        ("arming_state", 2),
        ("velocity_ned", [1, 0, 0]),
        ("battery_remaining", 0.1),
        ("battery_remaining", float("nan")),
    ],
)
def test_return_requires_actual_stable_deck_recovery(field, value):
    config, samples, events = evidence()
    samples[-1][field] = value
    report = verify_ship_sitl_run(samples, events, config)
    assert not report["verified"]
    assert report["delivery_verified"]


def test_missing_return_dispatch_cannot_be_replaced_by_recovery_position():
    config, samples, events = evidence()
    events = [e for e in events if e["event"] != "return_requested"]
    assert not verify_ship_sitl_run(samples, events, config)["verified"]


@pytest.mark.parametrize("mutation", ["order", "permit", "hash", "count", "wind", "run"])
def test_unbound_or_changed_runtime_evidence_blocks_completion(mutation):
    config, samples, events = evidence()
    permit = next(e for e in events if e["event"] == "return_authorized")["binding"]
    if mutation == "order":
        events.reverse()
    elif mutation == "permit":
        permit["plan_sha256"] = "other"
    elif mutation == "hash":
        permit["evidence_sha256"] = "changed"
    elif mutation == "count":
        permit["evidence_sample_count"] = 1
    elif mutation == "wind":
        next(e for e in events if e["event"] == "wind_observed")["readback"] = ""
    else:
        events[-1]["run_id"] = "other"
    assert not verify_ship_sitl_run(samples, events, config)["verified"]


def test_jsonl_only_tolerates_an_incomplete_final_write(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    path.write_text('{"ok": true}\n{"unfinished":')
    assert _read_jsonl(path) == [{"ok": True}]
    path.write_text('{"broken":\n')
    with pytest.raises(ValueError, match="Corrupt complete"):
        _read_jsonl(path)


def test_deck_contact_solver_tolerance_still_requires_contact_and_stability():
    config, samples, events = evidence()
    for row in samples:
        if row["landed"]:
            row["vehicle"]["xyz"][2] = -0.0002
    assert verify_ship_sitl_run(samples, events, config)["verified"]
    samples[-1]["vehicle"]["xyz"][2] = -0.1
    assert not verify_ship_sitl_run(samples, events, config)["verified"]


def test_simulated_recharge_after_disarm_cannot_hide_insufficient_flight_reserve():
    config, samples, events = evidence()
    next(r for r in samples if r["phase"] == "return" and not r["landed"])["battery_remaining"] = (
        0.1
    )
    report = verify_ship_sitl_run(samples, events, config)
    assert report["minimum_simulated_battery_remaining"] == 0.1
    assert "return_battery_reserve_not_verified" in report["reasons"]
