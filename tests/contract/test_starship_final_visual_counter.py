"""Counter observation/release contracts; scripted clocks, no flight or model.

Uses real ConfirmationChannel sampling/arrival and actual algebraic rigid-release
receipts. Scripted timestamps are a boundary fixture, never propagated flight.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import socket
import subprocess

import pytest

from src.runtime import starship_final_verifier as final
from src.runtime import starship_sixdof as dyn, starship_physics as env
from src.runtime.starship_anomaly_events import ConfirmationChannel
from src.runtime.starship_final_visual import FinalVisualPolicy
from tests.contract.test_starship_final_visual import Plant


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@pytest.fixture(autouse=True)
def no_flight_provider_or_process(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Counter timeline contracts cannot propagate, call providers, or spawn processes")
    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"), (env, "step"),
            (socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def make_counter_timeline(*, terminal_before_observer=True):
    """Observe BEFORE each same-clock release, then see its result next tick."""
    plant = Plant()
    run_id = "final-counter-order-test"
    policy = FinalVisualPolicy().record()
    channel = ConfirmationChannel(run_id, first_delay_s=policy["first_counter_delay_s"])
    ticks, events = [], []
    release_times = {102.+2*index for index in range(13)}
    for index in range(131):
        now = 100.+index*.25
        plant.state = replace(plant.state, time_s=now)
        count = len(plant.records)
        ticks.append({"sequence": index, "body_id": "ship", "time_s": now, "next_time_s": now+.25,
            "phase": "orbital_coast", "released_count": count, "sequencer_state": "running",
            "deployment_withheld": False})
        if count == 13 and channel.current_attempt is None:
            attempt = channel.attempt(now, count)
            events.append({"event": "release_ack_observed", **attempt, "separation_inferred_from_ack": False})
        channel.observe(now, count)
        if now in release_times:
            # The same algebraic finite-body release function used by the
            # production plant creates a real conserved-momentum receipt.
            plant.release()
    # A pre-observer terminal tick arrives exactly when a pending counter
    # sample would become available. The producer exits before observing it.
    terminal_time = ticks[-1]["next_time_s"]
    plant.state = replace(plant.state, time_s=terminal_time)
    if not terminal_before_observer:
        channel.observe(terminal_time, len(plant.records))
    ticks.append({"sequence": len(ticks), "body_id": "ship", "time_s": terminal_time,
        "next_time_s": terminal_time, "phase": "orbital_coast", "released_count": len(plant.records),
        "sequencer_state": "skipped", "deployment_withheld": False})
    receipts = [deepcopy(row["receipt"]) for row in plant.records]
    inventory = {"schema": "missionos.starship_restored_inventory.v1", "total_count": 26,
        "released_count": 13, "retained_count": 13, "generation": 13,
        "sequencer_state": "skipped", "pending_action": None,
        "observed_time_s": plant.state.time_s, "release_receipts": receipts}
    prefix = {"concurrent_execution": {"schema": "missionos.ship_booster_clock.v1", "tick_order": ticks,
            "observers_run_before_each_body_step": True, "posthoc_ordering": False},
        "satellites": deepcopy(plant.records), "final_state": asdict(plant.state),
        "final_vehicle": asdict(plant.craft),
        "events": [{"event": "payload_released", "time_s": row["time_s"], "receipt": deepcopy(row)} for row in receipts]
            + [{"event": "restored_deployment_handoff", "time_s": terminal_time,
                "detail": ("actual inventory already closed; same actual coast state retained" if terminal_before_observer
                    else "actual inventory closed; same state retained before nominal return"), "state_reset": False}],
        "outcome": {"payload_released_count": 13, "termination": "restored_deployment_handoff", "phase": "orbital_coast"},
        "restored_prefix": {"closed": True, "handoff_record": {"inventory": deepcopy(inventory)}}}
    value = {"counter_attempts": deepcopy(channel.attempts), "counter_receipts": deepcopy(channel.receipts),
        "events": events}
    inputs = {"context": {"run_id": run_id}, "visual_policy": policy}
    return value, prefix, inventory, inputs


@pytest.fixture
def counter_timeline():
    return make_counter_timeline()


def check(case):
    value, prefix, inventory, inputs = case
    return final._verify_visual_counter_timeline(value, prefix=prefix, inventory=inventory, inputs=inputs)


def test_counter_ack_observation_is_next_exact_tick_after_real_release(counter_timeline):
    value, prefix, inventory, _ = counter_timeline
    before = digest(counter_timeline)
    attempt = value["counter_attempts"][0]
    action = inventory["release_receipts"][12]
    assert action["time_s"] == 126.0 and attempt["source_time_s"] == 126.25
    assert attempt["source_time_s"] not in {row["time_s"] for row in inventory["release_receipts"]}
    incoming = next(row for row in prefix["concurrent_execution"]["tick_order"] if row["time_s"] == 126.0)
    observed = next(row for row in prefix["concurrent_execution"]["tick_order"] if row["time_s"] == 126.25)
    assert incoming["released_count"] == 12 and observed["released_count"] == attempt["expected_count"] == 13
    check(counter_timeline)
    assert digest(counter_timeline) == before  # No raw-record or clock rewriting.


def test_same_clock_counter_sample_precedes_actual_release_action(counter_timeline):
    value, _, inventory, _ = counter_timeline
    action_time = inventory["release_receipts"][-1]["time_s"]
    sample = next(row for row in value["counter_receipts"] if row["source_time_s"] == action_time)
    assert sample["confirmed_count"] == 12
    # This explicitly demonstrates why <=release_time was the wrong oracle.
    assert sum(row["time_s"] <= action_time for row in inventory["release_receipts"]) == 13
    check(counter_timeline)


def test_pre_observer_terminal_tick_does_not_deliver_one_more_counter_sample(counter_timeline):
    value, prefix, _, _ = counter_timeline
    terminal = prefix["final_state"]["time_s"]
    assert terminal == 132.75
    assert all(row["available_time_s"] < terminal for row in value["counter_receipts"])
    # Observing that terminal tick would deliver the pending132.25 sample and
    # incorrectly change the saved receipt ledger. The actual path exited first.
    assert not any(row["source_time_s"] == 132.25 for row in value["counter_receipts"])
    check(counter_timeline)


def test_post_observer_terminal_tick_retains_its_arrived_counter_receipt():
    case = make_counter_timeline(terminal_before_observer=False)
    value, prefix, _, _ = case
    terminal = prefix["final_state"]["time_s"]
    assert any(row["source_time_s"] == 132.25 and row["available_time_s"] == terminal
        for row in value["counter_receipts"])
    assert prefix["events"][-1]["detail"] == "actual inventory closed; same state retained before nominal return"
    check(case)


def rehash_receipt(receipt):
    receipt["receipt_sha256"] = digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})


def rebind_observed_ack(value):
    value["events"][0] = {"event": "release_ack_observed", **deepcopy(value["counter_attempts"][0]),
        "separation_inferred_from_ack": False}


@pytest.mark.parametrize("change", ["action_time_as_observation", "off_actual_tick", "later_observation_tick",
    "wrong_release_action_time", "wrong_observed_count", "wrong_previous_count", "promote_same_clock_sample",
    "attempt_id", "expected_count", "reordered_receipts", "duplicate_receipt", "sequence_gap", "early_arrival",
    "wrong_counter_attempt_id", "changed_rigid_receipt", "ack_event", "omitted_attempt",
    "extra_ack", "early_available_after_source", "sampling_cadence", "terminal_event_missing", "terminal_detail"])
def test_counter_timeline_rejects_wrong_clock_count_identity_or_arrival(counter_timeline, change):
    value, prefix, inventory, _ = counter_timeline
    attempt = value["counter_attempts"][0]
    ticks = prefix["concurrent_execution"]["tick_order"]
    if change in ("action_time_as_observation", "off_actual_tick", "later_observation_tick"):
        attempt["source_time_s"] = {"action_time_as_observation": 126.0,
            "off_actual_tick": 126.125, "later_observation_tick": 126.5}[change]
        rebind_observed_ack(value)
    elif change == "wrong_release_action_time":
        # Consistently forge both copies within the same tick interval. A
        # tolerance/window-only join would accept this; the exact contract must not.
        receipt = inventory["release_receipts"][-1]
        receipt["time_s"] = receipt["parent_before_state"]["time_s"] = receipt["parent_after_state"]["time_s"] = 126.125
        prefix["satellites"][-1]["receipt"] = deepcopy(receipt)
    elif change == "wrong_observed_count":
        next(row for row in ticks if row["time_s"] == 126.25)["released_count"] = 12
    elif change == "wrong_previous_count":
        next(row for row in ticks if row["time_s"] == 126.)["released_count"] = 13
    elif change == "promote_same_clock_sample":
        receipt = next(row for row in value["counter_receipts"] if row["source_time_s"] == 126.)
        receipt["confirmed_count"] = 13
        rehash_receipt(receipt)
    elif change == "attempt_id":
        attempt["release_attempt_id"] = "a-forged"
        rebind_observed_ack(value)
        for receipt in value["counter_receipts"]:
            if receipt["release_attempt_id"] is not None:
                receipt["release_attempt_id"] = attempt["release_attempt_id"]
                rehash_receipt(receipt)
    elif change == "expected_count":
        attempt["expected_count"] = 14
        rebind_observed_ack(value)
    elif change == "reordered_receipts":
        value["counter_receipts"][0], value["counter_receipts"][1] = value["counter_receipts"][1], value["counter_receipts"][0]
    elif change == "duplicate_receipt":
        value["counter_receipts"].insert(1, deepcopy(value["counter_receipts"][0]))
    elif change in ("sequence_gap", "early_arrival"):
        receipt = value["counter_receipts"][0]
        receipt["source_sequence" if change == "sequence_gap" else "available_time_s"] = 2 if change == "sequence_gap" else receipt["source_time_s"]-.25
        rehash_receipt(receipt)
    elif change == "wrong_counter_attempt_id":
        receipt = next(row for row in value["counter_receipts"] if row["source_time_s"] > attempt["source_time_s"])
        receipt["release_attempt_id"] = "a-other"
        rehash_receipt(receipt)
    elif change == "changed_rigid_receipt":
        prefix["satellites"][-1]["receipt"]["time_s"] -= .25
    elif change == "omitted_attempt":
        # Coherently remove tokens and ack too; reproducing the actual producer
        # from the physical ticks must still recreate the missing attempt.
        value["counter_attempts"], value["events"] = [], []
        for receipt in value["counter_receipts"]:
            receipt["release_attempt_id"] = None
            rehash_receipt(receipt)
    elif change == "extra_ack":
        value["events"].append({"event": "release_ack_observed", **deepcopy(attempt),
            "release_attempt_id": "a-orphan", "separation_inferred_from_ack": False})
    elif change == "early_available_after_source":
        receipt = next(row for row in value["counter_receipts"]
            if row["release_attempt_id"] == attempt["release_attempt_id"])
        assert receipt["available_time_s"]-receipt["source_time_s"] == 4.
        # Still source<=arrival, sequence and count consistent. Only exact
        # producer replay catches removal of the registered transport barrier.
        receipt["available_time_s"] = receipt["source_time_s"]+.5
        rehash_receipt(receipt)
    elif change == "sampling_cadence":
        receipt = value["counter_receipts"][2]
        assert receipt["source_time_s"] == 101.
        receipt["source_time_s"], receipt["available_time_s"] = 101.25, 101.75
        rehash_receipt(receipt)
    elif change == "terminal_event_missing":
        prefix["events"] = prefix["events"][:-1]
    elif change == "terminal_detail":
        prefix["events"][-1]["detail"] = "arbitrary producer claim to skip final observation"
    else:
        value["events"][0]["expected_count"] = 12
    with pytest.raises(ValueError):
        check(counter_timeline)
