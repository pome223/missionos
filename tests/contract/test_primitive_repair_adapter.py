from dataclasses import replace

import pytest

from src.runtime.causal_repair_candidates import Candidate, Primitive
from src.runtime.candidate_informativeness import Start
from src.runtime.primitive_repair_adapter import PrimitiveRepairAdapter, RobotObservation


class Session:
    contract_digest = "test-controller"
    evidence_kind = "fixture"

    def __init__(self):
        self.calls = []
        self.current = RobotObservation(
            (0.0, 0.0, 0.0), (0.0, 0.0, 0.1), True, True, True, (False,), 0.03
        )

    def restore(self, start):
        return start.snapshot_digest

    def observe(self):
        return self.current

    def step(self, delta, grip):
        self.calls.append((delta, grip))
        hand = tuple(a + b for a, b in zip(self.current.hand_m, delta))
        obj = (
            tuple(a + b for a, b in zip(self.current.object_m, delta))
            if grip == 1
            else self.current.object_m
        )
        self.current = replace(
            self.current, hand_m=hand, object_m=obj, held=grip == 1, predicates=(grip == -1,)
        )


def setup(operations):
    candidate = Candidate("test", "test", "start", "hypothesis", tuple(operations))
    start = Start("case", "start", (candidate,))
    session = Session()
    adapter = PrimitiveRepairAdapter(session, allow_simulator=True)
    adapter.restore(start)
    return session, adapter, start, candidate


def test_optin_and_fresh_restore_are_required():
    with pytest.raises(ValueError, match="opt-in"):
        PrimitiveRepairAdapter(Session())
    s, a, start, c = setup([Primitive("translate", "object", (0.0, 0.0, 0.0), 10)])
    assert a.evidence_kind == "fixture"
    a.execute(start, c, 10)
    with pytest.raises(ValueError, match="restore"):
        a.execute(start, c, 10)


def test_motion_is_bounded_and_release_stays_open():
    ops = [
        Primitive("translate", "object", (0.02, 0.0, 0.0), 10),
        Primitive("place_then_stabilize", "object", (0.02, 0.0, 0.0), 10),
        Primitive("release", "object", (0.02, 0.0, 0.0), 40),
        Primitive("hold_and_observe", "object", (0.02, 0.0, 0.0), 30),
    ]
    s, a, start, c = setup(ops)
    row = a.execute(start, c, 90)
    assert row.predicates == (True,)
    assert row.completion_steps == len(s.calls)
    assert all(abs(v) <= 0.006 for delta, _ in s.calls for v in delta)
    opening = next(i for i, (_, grip) in enumerate(s.calls) if grip == -1)
    assert all(grip == -1 for _, grip in s.calls[opening:])


def test_unsupported_regrasp_never_calls_robot():
    s, a, start, c = setup([Primitive("acquire_opposite_side", "object", (0.0, 0.0, 0.0), 10)])
    with pytest.raises(ValueError, match="unsupported"):
        a.execute(start, c, 10)
    assert not s.calls


def test_release_requires_observed_support():
    s, a, start, c = setup([Primitive("release", "object", (0.0, 0.0, 0.0), 30)])
    s.current = replace(s.current, supported=False)
    a.execute(start, c, 30)
    assert not s.calls
    assert a.stop_reasons[(start.start_id, c.family)] == "release_without_support_denied"


def test_lost_grasp_stops_before_motion():
    s, a, start, c = setup([Primitive("translate", "object", (0.1, 0.0, 0.0), 30)])
    s.current = replace(s.current, held=False)
    a.execute(start, c, 30)
    assert not s.calls
    assert a.stop_reasons[(start.start_id, c.family)] == "grasp_lost"


def test_preservation_loss_stops_next_control_and_is_latched():
    s, a, start, c = setup([Primitive("translate", "object", (0.1, 0.0, 0.0), 30)])
    step = s.step

    def violate(delta, grip):
        step(delta, grip)
        s.current = replace(s.current, preserved=False)

    s.step = violate
    row = a.execute(start, c, 30)
    assert len(s.calls) == 1
    assert row.preservation is False


def test_primitive_timeout_is_observed_not_infrastructure_failure():
    s, a, start, c = setup([Primitive("translate", "object", (0.9, 0.0, 0.0), 3)])
    row = a.execute(start, c, 3)
    assert row.status == "observed"
    assert row.completion_steps == 3
    assert a.stop_reasons[(start.start_id, c.family)] == "primitive_timeout:translate"


def test_nonfinite_target_cannot_turn_into_a_robot_command():
    s, a, start, c = setup([Primitive("translate", "object", (float("nan"), 0.0, 0.0), 30)])
    with pytest.raises(ValueError):
        a.execute(start, c, 30)
    assert not s.calls
