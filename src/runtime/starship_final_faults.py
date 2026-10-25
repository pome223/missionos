"""Finite seeded test selection, consumed only at an actual dispatch handoff.

This harness-only module never builds provider input. It reuses the discrete
trouble engine; its probabilities are test settings, not incident rates. The
actual state and all actuator history remain unchanged at injection. Applying
a harness fault is not approval, dispatch, diagnosis or recovery.
"""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import math

SCHEMA = "missionos.starship_final_fault_selection.v1"
POOL = ("normal", "return_propulsion_80", "return_sensor_gain_80")
GENERATOR = "sha256_final_finite_pool_v1"


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(wire(value)).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def selection(seed):
    """Freeze this result before simulation; never search flight/AI outcomes."""
    from .starship_trouble_engine import make_manifest
    require(type(seed) is int and 0 <= seed < 2**63, "final_fault_seed")
    index = int.from_bytes(sha256(f"{GENERATOR}:{seed}".encode()).digest()[:8], "big") % len(POOL)
    family = POOL[index]
    manifest = make_manifest(seed, allowed_families=list(POOL[1:]) if family == "normal" else [family],
        max_events=0 if family == "normal" else 1, nominal_probability=0.)
    value = {"schema": SCHEMA, "seed": seed, "generator": GENERATOR, "pool": list(POOL),
        "selected_family": family, "manifest": manifest,
        "parameter_basis": "synthetic_test_distribution_not_historical_incident_rates",
        "phase": "accepted_return_dispatch", "harness_only": True,
        "provider_input": False, "candidate_selection_from_outcomes": False}
    return {**value, "selection_sha256": digest(value)}


def validate_selection(value):
    require(type(value) is dict and type(value.get("seed")) is int, "final_fault_selection")
    expected = selection(value["seed"])
    require(wire(value) == wire(expected), "final_fault_selection_changed")
    return deepcopy(expected)


def _apply(current, vehicle, selected):
    from .starship_trouble_engine import TroubleEngine
    before = wire(asdict(current))
    engine = TroubleEngine(selected["manifest"])
    after, plant = engine.before_step(current, vehicle, "ship", "accepted_return_dispatch")
    require(wire(asdict(after)) == before and after is current, "final_fault_changed_actual_state")
    audit = engine.audit()
    require(not audit["unapplied_event_ids"]
        and len(audit["receipts"]) == (0 if selected["selected_family"] == "normal" else 1),
        "final_fault_not_applied_exactly_once")
    return plant, engine


class FinalFaultHarness:
    """Private one-use test harness; no health-flag or model-facing interface."""

    __slots__ = ("_selection_json", "_expected_selection_sha256", "_used", "_receipt", "_engine")

    def __init__(self, selected, *, expected_selection_sha256):
        selected = validate_selection(selected)
        require(type(expected_selection_sha256) is str
            and expected_selection_sha256 == selected["selection_sha256"], "final_fault_registration_changed")
        self._selection_json = wire(selected)
        self._expected_selection_sha256 = expected_selection_sha256
        self._used, self._receipt, self._engine = False, None, None

    @property
    def selected(self):
        """Detached inspection only; changing it cannot edit the registered draw."""
        return json.loads(self._selection_json)

    def _bound_selection(self):
        require(type(self._selection_json) is bytes, "final_fault_registration_changed")
        selected = validate_selection(self.selected)
        require(wire(selected) == self._selection_json
            and selected["selection_sha256"] == self._expected_selection_sha256,
            "final_fault_registration_changed")
        return selected

    def consume_actual_handoff(self, handoff):
        from .starship_final_handoff import FinalActualReturnHandoff
        require(type(handoff) is FinalActualReturnHandoff and not self._used, "final_fault_actual_handoff_required")
        self._used = True  # Consumed failures cannot become an implicit retry.
        selected = self._bound_selection()  # Before the actual owner is consumed.
        proof = handoff.record()
        state, vehicle = handoff.consume()
        require(wire(proof.get("snapshot", {}).get("state")) == wire(asdict(state))
            and wire(proof.get("snapshot", {}).get("vehicle")) == wire(asdict(vehicle)),
            "final_fault_handoff_snapshot_mismatch")
        plant, engine = _apply(state, vehicle, selected)
        self._engine = engine
        self._receipt = {"schema": "missionos.starship_final_fault_injection.v1",
            "selection_sha256": selected["selection_sha256"], "handoff_sha256": digest(proof),
            "injection_time_s": state.time_s, "state_sha256": digest(asdict(state)),
            "before_vehicle_sha256": digest(asdict(vehicle)), "after_vehicle_sha256": digest(asdict(plant)),
            "engine_audit": engine.audit(), "state_reset": False, "actuator_reset": False,
            "provider_input": False, "harness_only": True, "execution_authority_granted": False}
        return state, plant

    def accelerometer_gain(self, time_s):
        require(self._engine is not None and self._receipt is not None
            and type(time_s) in (int, float) and math.isfinite(time_s)
            and time_s >= self._receipt["injection_time_s"],
            "final_fault_not_injected_or_clock_reversed")
        return self._engine.accelerometer_gain(time_s)

    def record(self):
        return deepcopy({"schema": "missionos.starship_final_fault_harness.v1", "selection": self.selected,
            "consumed": self._used, "injection": self._receipt, "provider_input": False})


def verify_injection(record, *, handoff_record, actual_state, before_vehicle, after_vehicle):
    """Recheck the discrete injector only; never reexecute flight or inference."""
    from .starship_sixdof import State6DOF, Vehicle6DOF
    require(type(actual_state) is State6DOF and type(before_vehicle) is Vehicle6DOF
        and type(after_vehicle) is Vehicle6DOF and type(handoff_record) is dict,
        "final_fault_verification_actual_inputs")
    require(type(record) is dict and record.get("schema") == "missionos.starship_final_fault_harness.v1"
        and record.get("consumed") is True and record.get("provider_input") is False,
        "final_fault_verification_record")
    selected = validate_selection(record.get("selection"))
    require(wire(handoff_record.get("snapshot", {}).get("state")) == wire(asdict(actual_state))
        and wire(handoff_record.get("snapshot", {}).get("vehicle")) == wire(asdict(before_vehicle)),
        "final_fault_handoff_snapshot_mismatch")
    plant, engine = _apply(actual_state, before_vehicle, selected)
    expected = {"schema": "missionos.starship_final_fault_injection.v1",
        "selection_sha256": selected["selection_sha256"], "handoff_sha256": digest(handoff_record),
        "injection_time_s": actual_state.time_s, "state_sha256": digest(asdict(actual_state)),
        "before_vehicle_sha256": digest(asdict(before_vehicle)), "after_vehicle_sha256": digest(asdict(plant)),
        "engine_audit": engine.audit(), "state_reset": False, "actuator_reset": False,
        "provider_input": False, "harness_only": True, "execution_authority_granted": False}
    require(wire(asdict(plant)) == wire(asdict(after_vehicle)) and wire(record.get("injection")) == wire(expected),
        "final_fault_injection_mismatch")
    return {"schema": "missionos.starship_final_fault_injection_check.v1", "passed": True,
        "discrete_replay_only": True, "flight_reexecuted": False, "model_calls": 0,
        "source_authentication_established": False, "execution_authority_granted": False}
