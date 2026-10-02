"""Exercise closed-loop primitive control with explicit synthetic kinematics."""

import argparse
from dataclasses import asdict, replace
import json

from src.runtime.causal_repair_candidates import Candidate, Primitive
from src.runtime.candidate_informativeness import Start
from src.runtime.primitive_repair_adapter import PrimitiveRepairAdapter, RobotObservation


class FixtureRobotSession:
    """A boundary fixture: no dynamics, simulator, hardware, or physical evidence."""

    contract_digest = "synthetic-kinematics-fixture-v1"
    evidence_kind = "fixture"

    def __init__(self):
        self.calls = []
        self.current = RobotObservation(
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.1),
            True,
            True,
            True,
            (False,),
            0.03,
        )

    def restore(self, start):
        return start.snapshot_digest

    def observe(self):
        return self.current

    def step(self, delta, grip):
        self.calls.append((delta, grip))
        hand = tuple(a + b for a, b in zip(self.current.hand_m, delta, strict=True))
        obj = (
            tuple(a + b for a, b in zip(self.current.object_m, delta, strict=True))
            if grip == 1
            else self.current.object_m
        )
        self.current = replace(
            self.current, hand_m=hand, object_m=obj, held=grip == 1, predicates=(grip == -1,)
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", required=True)
    parser.parse_args()
    session = FixtureRobotSession()
    adapter = PrimitiveRepairAdapter(session, allow_simulator=True)
    assert adapter.evidence_kind == "fixture"
    operations = tuple(
        Primitive(name, "object", (0.02, 0.0, 0.0), budget)
        for name, budget in (
            ("translate", 10),
            ("place_then_stabilize", 10),
            ("release", 40),
            ("hold_and_observe", 30),
        )
    )
    candidate = Candidate("direct", "transport", "fixture-start", "fixture", operations)
    start = Start("fixture", "fixture-start", (candidate,))
    assert adapter.restore(start) == start.snapshot_digest
    result = adapter.execute(start, candidate, 90)
    assert result.predicates == (True,) and result.preservation
    assert result.completion_steps == len(session.calls)
    try:
        adapter.execute(start, candidate, 90)
    except ValueError as error:
        assert "restore" in str(error)
    else:
        raise AssertionError("second arm incorrectly reused terminal state")
    print(
        json.dumps(
            {
                "evidence_kind": "fixture",
                "robotics_simulator_invoked": False,
                "result": asdict(result),
                "fresh_restore_guard": "passed",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
