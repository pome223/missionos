"""Exercise the grasp primitive through the opt-in adapter with a fixture robot."""

import argparse
from dataclasses import replace
import json

from src.runtime.causal_repair_candidates import Candidate, Primitive
from src.runtime.candidate_informativeness import Start
from src.runtime.grasp_recovery import GraspEvidence
from src.runtime.primitive_repair_adapter import PrimitiveRepairAdapter, RobotObservation


class FixtureSession:
    evidence_kind = "fixture"
    contract_digest = "fixture-grasp-control-v1"

    def restore(self, start):
        self.steps = 0
        self.calls = []
        self.obs = RobotObservation(
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.1),
            False,
            False,
            True,
            (False,),
            0.03,
            GraspEvidence(
                0.0,
                (1.0, 0.0),
                (0.001, 0.0, 0.0),
                (0.0, 0.0, -0.1),
                (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                True,
            ),
        )
        return start.snapshot_digest

    def observe(self):
        return self.obs

    def step(self, delta, gripper):
        self.steps += 1
        self.calls.append((delta, gripper))
        held = self.steps >= 3
        self.obs = replace(
            self.obs,
            held=held,
            hand_m=tuple(a + b for a, b in zip(self.obs.hand_m, delta)),
            grasp=replace(
                self.obs.grasp,
                time_seconds=self.steps * 0.05,
                pad_forces_n=(1.0, 1.0 if held else 0.0),
            ),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", required=True)
    parser.parse_args()
    session = FixtureSession()
    adapter = PrimitiveRepairAdapter(session, allow_simulator=True)
    c = Candidate(
        "recover_bilateral_grasp",
        "loaded_grasp_contact",
        "fixture-start",
        "fixture-unilateral-contact",
        (Primitive("recover_bilateral_grasp", "object", (0.0, 0.0, 0.0), 30),),
    )
    start = Start("fixture", "fixture-start", (c,))
    adapter.restore(start)
    receipt = adapter.execute(start, c, 30)
    assert adapter.stop_reasons[(start.start_id, c.family)] == "program_complete"
    assert receipt.predicates == (False,)  # qualification is not mission completion
    assert all(grip == 1.0 for _, grip in session.calls)
    print(
        json.dumps(
            {
                "evidence_kind": adapter.evidence_kind,
                "control_steps": session.steps,
                "grasp_qualified": True,
                "mission_complete": False,
            }
        )
    )


if __name__ == "__main__":
    main()
