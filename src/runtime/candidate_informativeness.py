"""Same-start exhaustive candidate evaluation, without any selector.

Backend receipts are trusted observations, not independently authenticated proof.
The evaluation must be conducted inside a separately authorized research scope.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Callable, Protocol

from src.runtime.causal_repair_candidates import Candidate, digest, finite


@dataclass(frozen=True)
class Start:
    start_id: str
    snapshot_digest: str
    candidates: tuple[Candidate, ...]
    rejected: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EvaluationPlan:
    starts: tuple[Start, ...]
    fixed_family: str
    maximum_steps: int
    predicate_names: tuple[str, ...]
    backend_contract_digest: str
    families: tuple[str, ...] = (
        "direct",
        "increase_clearance",
        "lateral_detour",
        "release_and_reacquire",
    )
    minimum_informativeness: float = 0.30
    minimum_oracle_gain: float = 0.10
    minimum_starts: int = 50

    @property
    def plan_digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class Measurement:
    start_id: str
    restored_digest: str
    program_digest: str
    status: str  # observed | infrastructure_error | unknown | reset_mismatch
    predicates: tuple[bool, ...] | None
    preservation: bool | None  # throughout execution, not only terminal pose
    completion_steps: int | None
    minimum_clearance_m: float | None
    object_displacement_m: float | None
    backend_contract_digest: str = ""


class EvaluationBackend(Protocol):
    """Adapter owns reset readback, bounded primitive execution and verification."""

    evidence_kind: str  # fixture | simulator
    contract_digest: str  # simulator/controller/primitive/verifier versions and semantics

    def restore(self, start: Start) -> str:
        """Restore state AND controller/RNG state; return actual readback digest."""
        ...

    def execute(self, start: Start, candidate: Candidate, maximum_steps: int) -> Measurement:
        """Execute only this program; record all protection violations and timeout."""
        ...


def validate_plan(plan: EvaluationPlan) -> None:
    if (
        not 1 <= len(plan.starts) <= 100
        or len({s.start_id for s in plan.starts}) != len(plan.starts)
        or len({s.snapshot_digest for s in plan.starts}) != len(plan.starts)
        or type(plan.maximum_steps) is not int
        or not 1 <= plan.maximum_steps <= 10000
        or type(plan.minimum_starts) is not int
        or not 50 <= plan.minimum_starts <= 100
        or not plan.predicate_names
        or not plan.backend_contract_digest
        or any(not n for n in plan.predicate_names)
        or len(set(plan.predicate_names)) != len(plan.predicate_names)
        or any(
            not finite(v) or not 0 < v <= 1
            for v in (plan.minimum_informativeness, plan.minimum_oracle_gain)
        )
    ):
        raise ValueError("invalid frozen evaluation plan")
    families = plan.families
    if (
        not 3 <= len(families) <= 5
        or len(set(families)) != len(families)
        or any(not family for family in families)
    ):
        raise ValueError("expected three to five distinct candidate families")
    if plan.fixed_family not in families:
        raise ValueError("fixed comparator missing")
    for start in plan.starts:
        attempted = [c.family for c in start.candidates]
        rejected = [family for family, _ in start.rejected]
        if (
            not start.start_id
            or not start.snapshot_digest
            or set(attempted + rejected) != set(families)
            or len(attempted + rejected) != len(families)
            or any(not reason for _, reason in start.rejected)
            or any(c.start_digest != start.snapshot_digest for c in start.candidates)
            or len({c.intervention for c in start.candidates}) != len(attempted)
            or any(
                not c.operations
                or not c.intervention
                or not c.hypothesis_ref
                or any(
                    type(op.maximum_steps) is not int or op.maximum_steps < 1 for op in c.operations
                )
                or sum(op.maximum_steps for op in c.operations) > plan.maximum_steps
                for c in start.candidates
            )
        ):
            raise ValueError("unmatched start, family, mechanism or action budget")


def evaluate_all(
    plan: EvaluationPlan,
    backend: EvaluationBackend,
    *,
    on_measurement: Callable[[Measurement], None] | None = None,
) -> tuple[Measurement, ...]:
    """Reset independently for EVERY candidate, including after success/error.

    Errors remain explicit missing outcomes. This function never retries or
    substitutes a start, and never stops early after a successful candidate.
    """
    validate_plan(plan)
    if backend.evidence_kind not in {"fixture", "simulator"}:
        raise ValueError("resettable research backend required")
    if backend.contract_digest != plan.backend_contract_digest:
        raise ValueError("backend contract differs from frozen plan")
    rows = []
    for start in plan.starts:
        for candidate in start.candidates:
            restored = ""
            try:
                restored = backend.restore(start)
                if restored != start.snapshot_digest:
                    row = Measurement(
                        start.start_id,
                        restored,
                        candidate.program_digest,
                        "reset_mismatch",
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
                else:
                    row = backend.execute(start, candidate, plan.maximum_steps)
            except Exception:
                # Do not leak adapter credentials or discard failed executions.
                row = Measurement(
                    start.start_id,
                    restored,
                    candidate.program_digest,
                    "infrastructure_error",
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            rows.append(row)
            if on_measurement is not None:
                on_measurement(row)
    return tuple(rows)


def run_recorded_evaluation(
    plan: EvaluationPlan,
    backend: EvaluationBackend,
    output_dir: Path,
) -> dict:
    """Persist the frozen plan first, then every result, including failed trials.

    Never overwrite an earlier attempt. A process interruption leaves the plan
    and completed JSONL rows available for auditing as an incomplete matrix.
    """
    validate_plan(plan)
    output_dir.mkdir(parents=True, exist_ok=False)
    frozen = {
        "plan": asdict(plan),
        "plan_digest": plan.plan_digest,
        "evidence_kind": backend.evidence_kind,
    }
    with (output_dir / "plan.json").open("x") as stream:
        json.dump(frozen, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    with (output_dir / "measurements.jsonl").open("x") as stream:

        def record(row):
            stream.write(json.dumps(asdict(row), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        rows = evaluate_all(plan, backend, on_measurement=record)
    result = summarize(plan, rows, evidence_kind=backend.evidence_kind)
    (output_dir / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


def summarize(
    plan: EvaluationPlan,
    measurements: tuple[Measurement, ...],
    *,
    evidence_kind: str,
) -> dict:
    """Compute headroom only from the complete, bound outcome matrix.

    Safe success = all terminal contract predicates AND preservation. The best
    fixed family is a hindsight diagnostic, a stronger check than the frozen
    fixed baseline. It is not a trained or deployed selector.
    """
    validate_plan(plan)
    if evidence_kind not in {"fixture", "simulator"}:
        raise ValueError("invalid evidence kind")
    expected = {(s.start_id, c.program_digest): (s, c) for s in plan.starts for c in s.candidates}
    rows = {}
    for row in measurements:
        key = (row.start_id, row.program_digest)
        if key not in expected or key in rows:
            raise ValueError("duplicate, unknown or unbound measurement")
        start, _ = expected[key]
        if row.status not in {"observed", "infrastructure_error", "unknown", "reset_mismatch"}:
            raise ValueError("invalid outcome status")
        if row.status == "observed" and (
            row.restored_digest != start.snapshot_digest
            or row.backend_contract_digest != plan.backend_contract_digest
            or row.predicates is None
            or len(row.predicates) != len(plan.predicate_names)
            or any(type(p) is not bool for p in row.predicates)
            or type(row.preservation) is not bool
            or type(row.completion_steps) is not int
            or not 0 <= row.completion_steps <= plan.maximum_steps
            or not finite(row.minimum_clearance_m)
            or not finite(row.object_displacement_m)
            or row.object_displacement_m < 0
        ):
            raise ValueError("invalid observed measurement or reset binding")
        rows[key] = row
    unknown = len(expected) - sum(r.status == "observed" for r in rows.values())
    violations = sum(r.preservation is False for r in rows.values())
    result = {
        "schema": "missionos.candidate_informativeness.v1",
        "plan_digest": plan.plan_digest,
        "evidence_kind": evidence_kind,
        "starts": len(plan.starts),
        "expected_trials": len(expected),
        "proposed_trials": len(plan.starts) * len(plan.families),
        "rejected_trials": sum(len(s.rejected) for s in plan.starts),
        "unknown_trials": unknown,
        "preservation_violations": violations,
        "candidate_informativeness": None,
        "predicate_vector_divergence": None,
        "oracle_success": None,
        "fixed_success": None,
        "best_fixed_success": None,
        "oracle_gain_over_fixed": None,
        "oracle_gain_over_best_fixed": None,
        "phase_b_admissible": False,
    }
    if unknown:
        result["reason"] = "incomplete_matrix"
        return result
    families = plan.families
    successes = dict.fromkeys(families, 0)
    oracle = divergent = vector_divergent = 0
    for start in plan.starts:
        outcomes, vectors = [], []
        for candidate in start.candidates:
            row = rows[(start.start_id, candidate.program_digest)]
            success = all(row.predicates) and row.preservation
            outcomes.append(success)
            vectors.append((row.predicates, row.preservation))
            successes[candidate.family] += int(success)
        oracle += int(any(outcomes))
        divergent += int(len(set(outcomes)) > 1)
        vector_divergent += int(len(set(vectors)) > 1)
    n = len(plan.starts)
    fixed, best_fixed = successes[plan.fixed_family], max(successes.values())
    admissible = (
        evidence_kind == "simulator"
        and n >= plan.minimum_starts
        and violations == 0
        and divergent / n >= plan.minimum_informativeness
        and (oracle - best_fixed) / n >= plan.minimum_oracle_gain
    )
    result.update(
        candidate_informativeness=divergent / n,
        predicate_vector_divergence=vector_divergent / n,
        oracle_success=oracle,
        fixed_success=fixed,
        best_fixed_success=best_fixed,
        family_successes=successes,
        oracle_gain_over_fixed=(oracle - fixed) / n,
        oracle_gain_over_best_fixed=(oracle - best_fixed) / n,
        phase_b_admissible=admissible,
        reason="headroom_gate_passed" if admissible else "headroom_not_qualified",
    )
    return result
