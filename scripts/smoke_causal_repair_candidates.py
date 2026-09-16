"""Run the proposal -> exhaustive reset -> measurement boundary with a fixture."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from src.runtime.causal_repair_candidates import (
    FAMILIES,
    FamilyProposal,
    RepairContext,
    digest,
    generate_candidates,
)
from src.runtime.candidate_informativeness import (
    EvaluationPlan,
    Measurement,
    Start,
    evaluate_all,
    summarize,
    run_recorded_evaluation,
)


OPERATIONS = (
    "translate",
    "raise",
    "lower",
    "place_then_stabilize",
    "release",
    "acquire_opposite_side",
    "hold_and_observe",
)
FIXTURE_CONTRACT = digest({"backend": "scripted-fixture-v1", "predicates": ["placed", "stable"]})


def fixture_plan() -> EvaluationPlan:
    starts = []
    for index in range(3):
        snapshot = digest({"fixture_start": index})
        context = RepairContext(
            snapshot,
            "book",
            (0.0, 0.0, 0.1),
            (0.2, 0.0, 0.1),
            (-0.1, 0.0, 0.1),
            (-1.0, -1.0, 0.0),
            (1.0, 1.0, 1.0),
            ("cup",),
            OPERATIONS,
            True,
            300,
        )
        candidates, rejected = generate_candidates(
            context,
            tuple(FamilyProposal(f, "fixture-hypothesis") for f in FAMILIES),
            feasible=lambda _: True,
        )
        assert not rejected
        starts.append(Start(str(index), snapshot, candidates))
    return EvaluationPlan(tuple(starts), "direct", 300, ("placed", "stable"), FIXTURE_CONTRACT)


class FixtureBackend:
    """Deliberately scripted outcomes; not a dynamics or robotics simulator."""

    evidence_kind = "fixture"
    contract_digest = FIXTURE_CONTRACT

    def __init__(self):
        self.restores = 0
        self.executions = 0

    def restore(self, start):
        self.restores += 1
        return start.snapshot_digest

    def execute(self, start, candidate, maximum_steps):
        self.executions += 1
        winner = FAMILIES[int(start.start_id)]
        success = candidate.family == winner
        return Measurement(
            start.start_id,
            start.snapshot_digest,
            candidate.program_digest,
            "observed",
            (success, success),
            True,
            100,
            0.02,
            0.2,
            self.contract_digest,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    plan, backend = fixture_plan(), FixtureBackend()
    if args.output:
        summary = run_recorded_evaluation(plan, backend, args.output)
        assert backend.restores == backend.executions == 12
        assert not summary["phase_b_admissible"]
        print(json.dumps(summary, indent=2))
        return
    measurements = evaluate_all(plan, backend)
    summary = summarize(plan, measurements, evidence_kind=backend.evidence_kind)
    assert backend.restores == backend.executions == 12
    assert summary["oracle_success"] == 3
    assert summary["best_fixed_success"] == 1
    assert not summary["phase_b_admissible"]
    print(
        json.dumps(
            {"summary": summary, "measurements": [asdict(r) for r in measurements]}, indent=2
        )
    )


if __name__ == "__main__":
    main()
