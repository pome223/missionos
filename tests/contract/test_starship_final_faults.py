"""Private finite fault selection/injection; no dynamics, model or network."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from src.runtime import starship_final_faults as faults
from src.runtime import starship_sixdof as dyn
from src.runtime.starship_sixdof_mission import vehicle

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_flight_or_health_oracle(monkeypatch):
    from src.runtime.starship_trouble_engine import TroubleEngine
    def forbidden(*args, **kwargs):
        raise AssertionError("No flight integration or ideal health flag in finite harness contracts")
    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (TroubleEngine, "observation")):
        if hasattr(owner, name):
            monkeypatch.setattr(owner, name, forbidden)


def plant():
    profile = json.loads((ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_bytes())
    craft = vehicle(profile, payload_count=13)
    engines = tuple(dyn.EngineState(throttle=.17 if i < 3 else 0., available=i != 3)
        for i in range(len(craft.engines)))
    state = dyn.State6DOF(5000., (6600000., 0., 0.), (0., 7400., 0.), (1., 0., 0., 0.),
        (.001, -.001, .002), 100000., engines, tuple(.01 for _ in craft.aero_panels))
    return state, craft


def selections():
    found = {}
    for seed in range(100):
        row = faults.selection(seed)
        found.setdefault(row["selected_family"], row)
    assert set(found) == set(faults.POOL)
    return found


def test_finite_seeded_manifest_and_history_free_selection():
    for value in selections().values():
        assert faults.validate_selection(value) == value == faults.selection(value["seed"])
        assert value["harness_only"] is True and value["provider_input"] is False
        assert value["candidate_selection_from_outcomes"] is False
        assert value["phase"] == "accepted_return_dispatch"
        assert len(value["manifest"]["events"]) == (value["selected_family"] != "normal")


@pytest.mark.parametrize("change", ["family", "seed", "delay", "extra"])
def test_post_selection_changes_rejected_even_with_rehashed_outer_record(change):
    value = deepcopy(selections()["return_propulsion_80"])
    if change == "family":
        value["selected_family"] = "normal"
    elif change == "seed":
        value["seed"] += 1
    elif change == "delay":
        value["manifest"]["events"][0]["delay_s"] = 10.
    else:
        value["expected_success"] = True
    value["selection_sha256"] = faults.digest({k: v for k, v in value.items() if k != "selection_sha256"})
    with pytest.raises(ValueError):
        faults.validate_selection(value)


@pytest.mark.parametrize("family", faults.POOL)
def test_discrete_injection_preserves_state_inventory_and_unavailable_engine(family):
    state, craft = plant()
    before_state, before_vehicle = asdict(state), asdict(craft)
    selected = selections()[family]
    result, engine = faults._apply(state, craft, selected)
    assert asdict(state) == before_state and asdict(craft) == before_vehicle
    assert result.dry_mass_kg == craft.dry_mass_kg
    assert result.dry_inertia_kg_m2 == craft.dry_inertia_kg_m2
    assert state.engine_states[3].available is False
    expected_factor = .8 if family == "return_propulsion_80" else 1.
    assert [e.max_thrust_n for e in result.engines[:3]] == [e.max_thrust_n*expected_factor for e in craft.engines[:3]]
    assert result.engines[3:] == craft.engines[3:]
    assert engine.accelerometer_gain(state.time_s) == (.8 if family == "return_sensor_gain_80" else 1.)
    assert not engine.audit()["unapplied_event_ids"]


def test_raw_or_predicted_state_cannot_be_consumed_as_an_actual_owner_handoff():
    # Class import occurs only on explicit injection, after the seam is installed.
    state, craft = plant()
    selected = selections()["return_propulsion_80"]
    harness = faults.FinalFaultHarness(selected, expected_selection_sha256=selected["selection_sha256"])
    for wrong in (asdict(state), (state, craft), {"prediction_only": True, "state": asdict(state)}):
        with pytest.raises(ValueError, match="actual_handoff_required"):
            harness.consume_actual_handoff(wrong)
    assert harness.record()["consumed"] is False


@pytest.mark.parametrize("seed", [True, -1, 2**63, 1.5, "1"])
def test_invalid_seed_does_not_create_a_trial(seed):
    with pytest.raises(ValueError):
        faults.selection(seed)


def test_registered_selection_and_injection_order_required():
    value = selections()["normal"]
    with pytest.raises(ValueError, match="registration_changed"):
        faults.FinalFaultHarness(value, expected_selection_sha256="f"*64)
    harness = faults.FinalFaultHarness(value, expected_selection_sha256=value["selection_sha256"])
    with pytest.raises(ValueError, match="not_injected"):
        harness.accelerometer_gain(1.)


def handoff_fixture(state, craft):
    # Synthetic sealed token exercises only the injection seam. It is not a
    # production actor, authenticated transcript, registered flight or authority.
    from src.runtime.starship_final_handoff import FinalActualReturnHandoff, _OWNER_SEAL
    record = {"fixture_only": True, "snapshot": {"state": asdict(state), "vehicle": asdict(craft)}}
    return FinalActualReturnHandoff(_seal=_OWNER_SEAL, state=state, vehicle=craft, record=record)


def test_post_init_manifest_mutation_is_detached_and_does_not_change_registered_fault():
    state, craft = plant()
    cases = selections()
    selected = cases["return_propulsion_80"]
    harness = faults.FinalFaultHarness(selected, expected_selection_sha256=selected["selection_sha256"])
    registered = harness.selected
    inspected = harness.selected
    inspected["manifest"] = cases["return_sensor_gain_80"]["manifest"]
    inspected["selected_family"] = "return_sensor_gain_80"
    # Mutation through either the original caller input or an inspected copy is
    # inert. No operation can turn this registered propulsion case into a sensor.
    selected["manifest"] = cases["return_sensor_gain_80"]["manifest"]
    assert harness.selected == registered
    handoff = handoff_fixture(state, craft)
    returned, after = harness.consume_actual_handoff(handoff)
    assert returned is state and handoff.consumed is True
    assert after.engines[0].max_thrust_n == craft.engines[0].max_thrust_n*.8
    assert harness.accelerometer_gain(state.time_s) == 1.
    record = harness.record()
    assert record["injection"]["engine_audit"]["receipts"][0]["family"] == "return_propulsion_80"
    assert faults.verify_injection(record, handoff_record=handoff.record(), actual_state=state,
        before_vehicle=craft, after_vehicle=after)["passed"] is True


def test_registered_selection_property_cannot_be_rebound():
    cases = selections()
    selected = cases["return_propulsion_80"]
    harness = faults.FinalFaultHarness(selected, expected_selection_sha256=selected["selection_sha256"])
    with pytest.raises(AttributeError):
        harness.selected = cases["return_sensor_gain_80"]
    assert harness.record()["selection"] == selected
    assert harness.record()["consumed"] is False


@pytest.mark.parametrize("corruption", ["manifest_only", "other_valid_draw", "expected_hash"])
def test_private_rebinding_is_rechecked_before_owner_consumption_and_failure_is_spent(corruption):
    state, craft = plant()
    cases = selections()
    selected = cases["return_propulsion_80"]
    harness = faults.FinalFaultHarness(selected, expected_selection_sha256=selected["selection_sha256"])
    if corruption == "manifest_only":
        altered = deepcopy(selected)
        altered["manifest"] = cases["return_sensor_gain_80"]["manifest"]
        harness._selection_json = faults.wire(altered)
    elif corruption == "other_valid_draw":
        harness._selection_json = faults.wire(cases["return_sensor_gain_80"])
    else:
        harness._expected_selection_sha256 = "f"*64
    handoff = handoff_fixture(state, craft)
    with pytest.raises(ValueError):
        harness.consume_actual_handoff(handoff)
    assert handoff.consumed is False
    assert harness.record()["consumed"] is True and harness.record()["injection"] is None
    with pytest.raises(ValueError, match="actual_handoff_required"):
        harness.consume_actual_handoff(handoff)


@pytest.mark.parametrize("family", faults.POOL)
def test_consumed_actual_handoff_binds_written_receipt_and_cannot_be_injected_twice(family):
    state, craft = plant()
    selected = selections()[family]
    harness = faults.FinalFaultHarness(selected, expected_selection_sha256=selected["selection_sha256"])
    handoff = handoff_fixture(state, craft)
    original = handoff.record()
    returned, after = harness.consume_actual_handoff(handoff)
    receipt = harness.record()["injection"]
    assert returned is state
    assert receipt["handoff_sha256"] == faults.digest(original)
    assert receipt["state_sha256"] == faults.digest(original["snapshot"]["state"])
    assert receipt["before_vehicle_sha256"] == faults.digest(original["snapshot"]["vehicle"])
    assert receipt["after_vehicle_sha256"] == faults.digest(asdict(after))
    assert receipt["injection_time_s"] == state.time_s
    assert faults.verify_injection(harness.record(), handoff_record=original,
        actual_state=state, before_vehicle=craft, after_vehicle=after)["passed"] is True
    with pytest.raises(ValueError, match="actual_handoff_required"):
        harness.consume_actual_handoff(handoff)
    with pytest.raises(ValueError, match="already_consumed"):
        handoff.consume()


@pytest.mark.parametrize("mutation", ["state", "vehicle", "receipt_state", "receipt_vehicle", "receipt_time"])
def test_snapshot_and_written_injection_receipt_mismatch_rejected(mutation):
    state, craft = plant()
    selected = selections()["return_propulsion_80"]
    harness = faults.FinalFaultHarness(selected, expected_selection_sha256=selected["selection_sha256"])
    handoff = handoff_fixture(state, craft)
    returned, after = harness.consume_actual_handoff(handoff)
    proof, record = handoff.record(), harness.record()
    if mutation == "state":
        proof["snapshot"]["state"]["propellant_kg"] -= 1.
    elif mutation == "vehicle":
        proof["snapshot"]["vehicle"]["dry_mass_kg"] -= 1.
    elif mutation == "receipt_state":
        record["injection"]["state_sha256"] = "f"*64
    elif mutation == "receipt_vehicle":
        record["injection"]["after_vehicle_sha256"] = "f"*64
    else:
        record["injection"]["injection_time_s"] += 1.
    with pytest.raises(ValueError, match="mismatch"):
        faults.verify_injection(record, handoff_record=proof, actual_state=returned,
            before_vehicle=craft, after_vehicle=after)


@pytest.mark.parametrize("mutation", ["state", "vehicle"])
def test_mismatched_owner_record_stops_before_injection_and_keeps_failed_harness_spent(monkeypatch, mutation):
    state, craft = plant()
    selected = selections()["return_propulsion_80"]
    harness = faults.FinalFaultHarness(selected, expected_selection_sha256=selected["selection_sha256"])
    handoff = handoff_fixture(state, craft)
    original_record = type(handoff).record

    def changed_record(owner):
        value = original_record(owner)
        if mutation == "state":
            value["snapshot"]["state"]["propellant_kg"] -= 1.
        else:
            value["snapshot"]["vehicle"]["dry_mass_kg"] -= 1.
        return value

    monkeypatch.setattr(type(handoff), "record", changed_record)
    monkeypatch.setattr(faults, "_apply", lambda *a, **k: pytest.fail("mismatch reached injector"))
    with pytest.raises(ValueError, match="snapshot_mismatch"):
        harness.consume_actual_handoff(handoff)
    assert handoff.consumed is True
    assert harness.record()["consumed"] is True and harness.record()["injection"] is None
    with pytest.raises(ValueError, match="actual_handoff_required"):
        harness.consume_actual_handoff(handoff)
