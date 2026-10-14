"""Observed inventory/timing handoff tests; no flight, provider or admission."""

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from src.runtime.starship_deployment_handoff import (
    DeploymentTracker, bind_handoff, validate_handoff, verify_binding,
)
from src.runtime.starship_replanning import uncertainty_origins
from src.runtime.starship_sixdof import EngineState, State6DOF

AUTHORITY = "a" * 64
PROFILE = "b" * 64


def state(when=1001.25):
    return json.loads(json.dumps(asdict(State6DOF(
        when, (6678137.123, 0., 0.), (0., 7725.0023, 0.),
        (.5, -.5, .5, .5), (.0010012, 0., 0.), 100001.5,
        (EngineState(throttle=.01, gimbal_x_rad=.001),), (.003,),
    ))))


def observation(when, count, sequence="running", **extras):
    return {"time_s": when, "released_count": count, "sequencer_state": sequence, **extras}


def retained25():
    tracker = DeploymentTracker(26, AUTHORITY)
    tracker.observe(observation(1000., 0))
    tracker.observe(observation(1000.5, 1))
    tracker.command("stop_deployment", 1000.5)
    tracker.observe(observation(1001., 1, "skipped"))
    return tracker, tracker.freeze(state(), PROFILE)


def result(origin):
    return {"origin": deepcopy(origin), "prediction_is_execution": False, "runtime_admission": False}


def redigest(handoff):
    handoff["handoff_sha256"] = sha256(json.dumps(
        {k: v for k, v in handoff.items() if k != "handoff_sha256"},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def test_retained_count_exact_state_and_observed_release_time_are_preserved():
    tracker, handoff = retained25()
    assert handoff["retained_count"] == handoff["origin"]["retained_count"] == 25
    assert handoff["released_count"] == 1
    assert handoff["inventory_basis"] == "synthetic_confirmed_separation_counter_v1"
    assert handoff["execution_state"] == state()
    assert handoff["origin"]["state"]["r_eci_m"][0] == 6678137.
    assert handoff["origin"]["state"]["propellant_kg"] == 100000.
    assert handoff["release_observations"] == [
        {"released_count": 1, "observed_time_s": 1000.5, "previous_observation_time_s": 1000.}]
    assert not handoff["dispatch_authorized"]
    validate_handoff(handoff)
    tracker.require_current(handoff)


def test_all_released_running_queue_closes_without_a_stop_command():
    tracker = DeploymentTracker(26, AUTHORITY)
    for count in range(27):
        tracker.observe(observation(1000. + 15 * count, count))
    handoff = tracker.freeze(state(1390.25), PROFILE)
    assert handoff["retained_count"] == 0
    assert len(handoff["release_observations"]) == 26
    validate_handoff(handoff)


def test_ack_is_not_separation_and_delayed_observation_time_is_not_a_schedule():
    tracker = DeploymentTracker(26, AUTHORITY)
    tracker.observe(observation(1000., 0, release_acknowledged=True))
    tracker.observe(observation(1001., 0, "inhibited", release_acknowledged=True))
    handoff = tracker.freeze(state(), PROFILE)
    assert handoff["retained_count"] == 26 and handoff["release_observations"] == []
    delayed = DeploymentTracker(26, AUTHORITY)
    for when, count in ((1000., 0), (1005., 1), (1040., 2)):
        delayed.observe(observation(when, count))
    delayed.command("stop_deployment", 1040.)
    delayed.observe(observation(1040.25, 2, "skipped"))
    frozen = delayed.freeze(state(1040.5), PROFILE)
    assert [r["observed_time_s"] for r in frozen["release_observations"]] == [1005., 1040.]


@pytest.mark.parametrize("action", ["hold", "continue", "stop_deployment", "collect_status"])
def test_every_command_invalidates_before_inventory_changes(action):
    tracker, handoff = retained25()
    prior = tracker.generation
    tracker.command(action, 1001.25)
    assert tracker.generation == prior + 1
    with pytest.raises(ValueError):
        tracker.require_current(handoff)
    with pytest.raises(ValueError):
        tracker.freeze(state(1001.5), PROFILE)
    tracker.observe(observation(1001.5, 1, {"hold": "held", "continue": "running",
                                         "stop_deployment": "skipped", "collect_status": "held"}[action]))
    with pytest.raises(ValueError):
        tracker.require_current(handoff)


def test_pending_command_needs_later_matching_observation_and_duplicate_is_idempotent():
    tracker = DeploymentTracker(26, AUTHORITY)
    row = observation(1000., 0)
    tracker.observe(row)
    tracker.observe(deepcopy(row))
    assert len(tracker.history) == 1
    tracker.command("stop_deployment", 1000.)
    tracker.observe(row)
    tracker.observe(observation(1000.25, 0, "running"))
    with pytest.raises(ValueError):
        tracker.freeze(state(1000.5), PROFILE)
    tracker.observe(observation(1000.5, 0, "skipped"))
    validate_handoff(tracker.freeze(state(1000.75), PROFILE))


@pytest.mark.parametrize("sequence", ["held", "running"])
def test_open_or_resumable_sequence_cannot_be_frozen(sequence):
    tracker = DeploymentTracker(26, AUTHORITY)
    tracker.observe(observation(1000., 0, sequence))
    with pytest.raises(ValueError):
        tracker.freeze(state(1000.25), PROFILE)


def test_held_is_not_closed_even_when_inventory_is_empty():
    tracker = DeploymentTracker(1, AUTHORITY)
    tracker.observe(observation(1000., 0))
    tracker.observe(observation(1000.25, 1, "held"))
    with pytest.raises(ValueError):
        tracker.freeze(state(1000.5), PROFILE)


@pytest.mark.parametrize("when", [1000.99, 1003.002])
def test_execution_state_must_follow_a_recent_observation(when):
    tracker, _ = retained25()
    with pytest.raises(ValueError):
        tracker.freeze(state(when), PROFILE)


@pytest.mark.parametrize("row", [observation(999., 0), observation(1001., 2),
                                 observation(1001., True), observation(1001., 0, []),
                                 observation(float("nan"), 0), observation(1000., 1)])
def test_bad_observations_reject_without_changing_tracker(row):
    tracker = DeploymentTracker(26, AUTHORITY)
    tracker.observe(observation(1000., 0))
    before = tracker.history
    with pytest.raises(ValueError):
        tracker.observe(row)
    assert tracker.history == before and tracker.generation == 0


def test_counter_cannot_start_nonzero_or_decrease():
    tracker = DeploymentTracker(26, AUTHORITY)
    with pytest.raises(ValueError):
        tracker.observe(observation(1000., 1))
    tracker.observe(observation(1000., 0))
    tracker.observe(observation(1001., 1))
    with pytest.raises(ValueError):
        tracker.observe(observation(1002., 0))


@pytest.mark.parametrize("mutation", ["hash", "retained", "origin", "history", "state", "authority", "grant", "missing_actuator", "inventory_basis"])
def test_malicious_or_unissued_handoff_is_rejected(mutation):
    tracker, handoff = retained25()
    damaged = deepcopy(handoff)
    if mutation == "hash":
        damaged["handoff_sha256"] = "c" * 64
    elif mutation == "retained":
        damaged["retained_count"] = 0
    elif mutation == "origin":
        damaged["origin"]["retained_count"] = 0
    elif mutation == "history":
        damaged["history"][-1]["released_count"] = 2
    elif mutation == "state":
        damaged["execution_state"]["r_eci_m"][0] += .001
    elif mutation == "authority":
        damaged["authority_sha256"] = "c" * 64
    elif mutation == "grant":
        damaged["dispatch_authorized"] = True
    elif mutation == "missing_actuator":
        del damaged["execution_state"]["engine_states"]
    else:
        damaged["inventory_basis"] = "actual_flight_certified"
    if mutation != "hash":
        redigest(damaged)
    with pytest.raises(ValueError):
        tracker.require_current(damaged)


def test_state_history_and_result_are_copied_not_mutated():
    tracker = DeploymentTracker(26, AUTHORITY)
    row = observation(1000., 0, "inhibited")
    tracker.observe(row)
    source = state(1000.25)
    frozen = tracker.freeze(source, PROFILE)
    row["released_count"] = 9
    source["engine_states"][0]["throttle"] = .99
    history = tracker.history
    history.clear()
    before = deepcopy(frozen)
    raw = result(frozen["origin"])
    bound = bind_handoff(raw, frozen)
    assert "deployment_handoff" not in raw and frozen == before
    verify_binding(bound, frozen)
    tracker.require_current(frozen)


def test_center_and_registered_perturbations_bind_exactly():
    _, handoff = retained25()
    for origin in [handoff["origin"], *uncertainty_origins(handoff["origin"])]:
        bound = bind_handoff(result(origin), handoff, expected_origin=origin)
        verify_binding(bound, handoff, expected_origin=origin)
        assert bound["deployment_handoff"]["retained_count"] == 25
    with pytest.raises(ValueError):
        bind_handoff(bound, handoff, expected_origin=origin)


@pytest.mark.parametrize("mutation", ["inventory", "state", "nested", "expected", "binding", "claim"])
def test_wrong_or_foreign_forecast_cannot_acquire_handoff_binding(mutation):
    _, handoff = retained25()
    origin = deepcopy(handoff["origin"])
    raw = result(origin)
    if mutation == "inventory":
        raw["origin"]["retained_count"] = 0
    elif mutation == "state":
        raw["origin"]["state"]["time_s"] += .25
    elif mutation == "nested":
        wrong = deepcopy(origin)
        wrong["retained_count"] = 0
        raw["trials"] = [{"origin": wrong}]
    elif mutation == "expected":
        origin["state"]["r_eci_m"][0] += 123.
        raw = result(origin)
    elif mutation == "binding":
        bound = bind_handoff(raw, handoff)
        bound["deployment_handoff"]["retained_count"] = 0
        with pytest.raises(ValueError):
            verify_binding(bound, handoff)
        return
    else:
        raw["prediction_is_execution"] = True
    with pytest.raises(ValueError):
        bind_handoff(raw, handoff, expected_origin=origin)


def released_all():
    tracker = DeploymentTracker(1, AUTHORITY)
    tracker.observe(observation(1000., 0))
    tracker.observe(observation(1001., 1))
    return tracker.freeze(state(), PROFILE)


@pytest.mark.parametrize("target", ["retained_count", "state_zero"])
def test_boolean_zero_cannot_equal_registered_numerical_origin(target):
    handoff = released_all()
    wrong = deepcopy(handoff["origin"])
    if target == "retained_count":
        assert wrong["retained_count"] == 0
        wrong["retained_count"] = False
    else:
        assert wrong["state"]["v_eci_mps"][0] == 0.
        wrong["state"]["v_eci_mps"][0] = False
    # Both candidate origin and caller-supplied expected origin must be strict.
    with pytest.raises(ValueError, match="unregistered_handoff_origin"):
        bind_handoff(result(wrong), handoff, expected_origin=wrong)
    with pytest.raises(ValueError, match="forecast_handoff_origin_mismatch"):
        bind_handoff(result(wrong), handoff)
    bound = bind_handoff(result(handoff["origin"]), handoff)
    bound["origin"] = wrong
    with pytest.raises(ValueError, match="forecast_handoff_origin_mismatch"):
        verify_binding(bound, handoff)


def test_boolean_zero_cannot_equal_forecast_inventory_binding():
    handoff = released_all()
    bound = bind_handoff(result(handoff["origin"]), handoff)
    assert bound["deployment_handoff"]["retained_count"] == 0
    bound["deployment_handoff"]["retained_count"] = False
    with pytest.raises(ValueError, match="forecast_handoff_binding_mismatch"):
        verify_binding(bound, handoff)


def test_boolean_clock_does_not_bypass_duplicate_observation_validation():
    tracker = DeploymentTracker(26, AUTHORITY)
    tracker.observe(observation(1., 0))
    before = tracker.history
    with pytest.raises(ValueError, match="invalid_handoff_clock"):
        tracker.observe(observation(True, 0))
    assert tracker.history == before
