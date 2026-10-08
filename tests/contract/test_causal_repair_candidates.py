from dataclasses import replace

import pytest

from scripts.smoke_causal_repair_candidates import FixtureBackend, fixture_plan, OPERATIONS
from src.runtime.causal_repair_candidates import (
    FAMILIES,
    FamilyProposal,
    RepairContext,
    generate_candidates,
)
from src.runtime.candidate_informativeness import evaluate_all, summarize, run_recorded_evaluation


def context(**changes):
    base = RepairContext(
        "snapshot",
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
    return replace(base, **changes)


def generate(c=None, **kwargs):
    return generate_candidates(
        c or context(),
        tuple(FamilyProposal(f, "observed-overlap") for f in FAMILIES),
        feasible=kwargs.get("feasible", lambda _: True),
    )


def test_distinct_interventions_and_shared_terminal_operations():
    programs, rejected = generate()
    assert not rejected
    assert len({p.intervention for p in programs}) == 4
    assert len({p.program_digest for p in programs}) == 4
    assert all(p.operations[-3:] == programs[0].operations[-3:] for p in programs)
    assert programs[1].operations[0].target_m[2] == 0.16
    assert programs[2].operations[0].target_m[1] == 0.08
    assert programs[3].operations[2].operation == "acquire_opposite_side"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"protected_objects": ("book",)}, "protected_object"),
        ({"held": False}, "requires_observed_grasp"),
        ({"maximum_steps": 100}, "budget_exceeded"),
        ({"supported_operations": ()}, "unsupported_primitive"),
        ({"goal_m": (2.0, 0.0, 0.1)}, "outside_workspace"),
    ],
)
def test_inadmissible_programs_stay_visible(changes, reason):
    programs, rejected = generate(context(**changes))
    assert programs == ()
    assert set(rejected.values()) == {reason}


def test_unsupported_regrasp_is_not_silently_replaced_by_transport():
    programs, rejected = generate(context(supported_operations=OPERATIONS[:-2] + OPERATIONS[-1:]))
    assert len(programs) == 3
    assert rejected == {"release_and_reacquire": "unsupported_primitive"}


def test_unknown_feasibility_is_rejected():
    programs, rejected = generate(feasible=lambda _: None)
    assert not programs
    assert set(rejected.values()) == {"feasibility_not_confirmed"}


@pytest.mark.parametrize(
    "changes",
    [
        {"clearance_m": float("nan")},
        {"held": 1},
        {"maximum_steps": True},
        {"position_m": (float("inf"), 0.0, 0.0)},
    ],
)
def test_invalid_context(changes):
    with pytest.raises(ValueError):
        generate(context(**changes))


def test_scripted_headroom_is_never_robotics_qualification():
    plan, backend = fixture_plan(), FixtureBackend()
    rows = evaluate_all(plan, backend)
    result = summarize(plan, rows, evidence_kind="fixture")
    assert backend.restores == backend.executions == 12
    assert result["candidate_informativeness"] == 1
    assert result["oracle_gain_over_best_fixed"] == 2 / 3
    assert not result["phase_b_admissible"]


def test_one_dominant_family_has_divergence_but_no_selection_headroom():
    plan, backend = fixture_plan(), FixtureBackend()
    rows = evaluate_all(plan, backend)
    rows = tuple(replace(r, predicates=(i % 4 == 1,) * 2) for i, r in enumerate(rows))
    result = summarize(plan, rows, evidence_kind="simulator")
    assert result["candidate_informativeness"] == 1
    assert result["oracle_gain_over_best_fixed"] == 0
    assert not result["phase_b_admissible"]


def test_incomplete_matrix_never_drops_a_start_from_denominator():
    plan = fixture_plan()
    rows = evaluate_all(plan, FixtureBackend())
    result = summarize(plan, rows[:-1], evidence_kind="simulator")
    assert result["starts"] == 3
    assert result["unknown_trials"] == 1
    assert result["candidate_informativeness"] is None
    assert not result["phase_b_admissible"]


def test_reset_mismatch_prevents_execution_and_keeps_unknown():
    class BadReset(FixtureBackend):
        def restore(self, start):
            return "wrong-state"

    plan, backend = fixture_plan(), BadReset()
    rows = evaluate_all(plan, backend)
    assert backend.executions == 0
    assert {r.status for r in rows} == {"reset_mismatch"}
    assert summarize(plan, rows, evidence_kind="simulator")["unknown_trials"] == 12


def test_backend_exception_is_retained_and_next_arm_is_restored():
    class Broken(FixtureBackend):
        def execute(self, *args):
            raise RuntimeError("adapter failed")

    plan, backend = fixture_plan(), Broken()
    rows = evaluate_all(plan, backend)
    assert backend.restores == 12
    assert {r.status for r in rows} == {"infrastructure_error"}


@pytest.mark.parametrize(
    "change",
    [
        {"restored_digest": "other"},
        {"program_digest": "other"},
        {"predicates": (1, True)},
        {"preservation": None},
        {"completion_steps": 301},
        {"minimum_clearance_m": float("nan")},
    ],
)
def test_receipts_fail_closed(change):
    plan = fixture_plan()
    rows = evaluate_all(plan, FixtureBackend())
    with pytest.raises(ValueError):
        summarize(plan, (replace(rows[0], **change), *rows[1:]), evidence_kind="simulator")


def test_preservation_violation_is_not_a_safe_success():
    plan = fixture_plan()
    rows = evaluate_all(plan, FixtureBackend())
    rows = tuple(replace(r, preservation=False) for r in rows)
    result = summarize(plan, rows, evidence_kind="simulator")
    assert result["oracle_success"] == 0
    assert result["preservation_violations"] == 12
    assert not result["phase_b_admissible"]


def test_filtered_start_is_kept_and_rejection_is_not_a_terminal_outcome():
    plan = fixture_plan()
    start = plan.starts[0]
    start = replace(start, candidates=(), rejected=tuple((f, "infeasible") for f in plan.families))
    plan = replace(plan, starts=(start, *plan.starts[1:]))
    rows = evaluate_all(plan, FixtureBackend())
    result = summarize(plan, rows, evidence_kind="fixture")
    assert result["starts"] == 3
    assert result["candidate_informativeness"] == 2 / 3
    assert result["proposed_trials"] == 12
    assert result["expected_trials"] == 8
    assert result["rejected_trials"] == 4


def test_recording_precedes_execution_and_keeps_failures(tmp_path):
    import json

    folder = tmp_path / "attempt"
    plan = fixture_plan()
    inspected = []

    class InspectJournal(FixtureBackend):
        def execute(self, *args):
            frozen = json.loads((folder / "plan.json").read_text())
            inspected.append(
                (
                    frozen["plan_digest"] == plan.plan_digest,
                    len((folder / "measurements.jsonl").read_text().splitlines())
                    == self.restores - 1,
                )
            )
            raise RuntimeError("unknown adapter outcome")

    result = run_recorded_evaluation(plan, InspectJournal(), folder)
    assert result["unknown_trials"] == 12
    assert inspected == [(True, True)] * 12
    assert len((folder / "measurements.jsonl").read_text().splitlines()) == 12
    with pytest.raises(FileExistsError):
        run_recorded_evaluation(plan, FixtureBackend(), folder)


def test_changed_backend_contract_prevents_any_execution():
    plan, backend = fixture_plan(), FixtureBackend()
    backend.contract_digest = "changed-controller-or-verifier"
    with pytest.raises(ValueError, match="backend contract"):
        evaluate_all(plan, backend)
    assert backend.restores == backend.executions == 0


def test_qualification_arithmetic_requires_full_cohort_and_is_fixture_blocked():
    # Synthetic records exercise the gate's positive branch, not simulator evidence.
    from src.runtime.causal_repair_candidates import digest

    base = fixture_plan()
    starts = []
    for i in range(50):
        source = base.starts[i % 3]
        snapshot = digest({"unit-test-start": i})
        starts.append(
            replace(
                source,
                start_id=str(i),
                snapshot_digest=snapshot,
                candidates=tuple(replace(c, start_digest=snapshot) for c in source.candidates),
            )
        )
    plan = replace(base, starts=tuple(starts))

    class SyntheticRecords(FixtureBackend):
        def execute(self, start, candidate, maximum_steps):
            row = super().execute(
                replace(start, start_id=str(int(start.start_id) % 3)), candidate, maximum_steps
            )
            return replace(row, start_id=start.start_id)

    rows = evaluate_all(plan, SyntheticRecords())
    assert summarize(plan, rows, evidence_kind="simulator")["phase_b_admissible"]
    assert not summarize(plan, rows, evidence_kind="fixture")["phase_b_admissible"]
    damaged = (replace(rows[0], preservation=False), *rows[1:])
    assert not summarize(plan, damaged, evidence_kind="simulator")["phase_b_admissible"]


def test_unilateral_recovery_has_separate_precondition_and_capability():
    proposal = (FamilyProposal("recover_bilateral_grasp", "observed-one-pad"),)
    c = context(
        held=False,
        unilateral_grasp_observed=True,
        supported_operations=(*OPERATIONS, "recover_bilateral_grasp"),
    )
    programs, rejected = generate_candidates(c, proposal, feasible=lambda _: True)
    assert rejected == {"direct": "requires_observed_grasp"}
    assert len(programs) == 1
    assert programs[0].operations[0].operation == "recover_bilateral_grasp"
    assert programs[0].operations[1].operation == "translate"
    for changes, reason in [
        ({"unilateral_grasp_observed": False}, "requires_observed_unilateral_grasp"),
        ({"supported_operations": OPERATIONS}, "unsupported_primitive"),
    ]:
        programs, rejected = generate_candidates(
            replace(c, **changes), proposal, feasible=lambda _: True
        )
        assert not programs
        assert rejected["recover_bilateral_grasp"] == reason
