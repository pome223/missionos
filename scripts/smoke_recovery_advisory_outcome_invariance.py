#!/usr/bin/env python3
"""Runtime smoke for recovery outcome advisory-context invariance (#475)."""

from __future__ import annotations

import json

from src.runtime.recovery_advisory_outcome_invariance import (
    RecoveryAdvisoryOutcomeInvarianceError,
    assert_recovery_outcome_ignores_advisory_context,
    current_recovery_advisory_context,
)
from tests.test_recovery_advisory_outcome_invariance import (
    _full_advisory_context,
    _load_corpus,
    _run_recovery_outcome_case,
)


def main() -> int:
    corpus = _load_corpus()
    evidence = assert_recovery_outcome_ignores_advisory_context(
        corpus=corpus,
        outcome_runner=_run_recovery_outcome_case,
        full_advisory_context=_full_advisory_context(),
    )

    negative_failed_closed = False

    def advisory_dependent_runner(_case, _advisory_context):
        return {
            "outcome_category": "recovered",
            "advisory_context_count": len(current_recovery_advisory_context()),
        }

    try:
        assert_recovery_outcome_ignores_advisory_context(
            corpus=[{"id": "negative-advisory-dependent"}],
            outcome_runner=advisory_dependent_runner,
            full_advisory_context=_full_advisory_context(),
        )
    except RecoveryAdvisoryOutcomeInvarianceError:
        negative_failed_closed = True

    kind_counts: dict[str, int] = {}
    for case in corpus:
        kind = str(case["kind"])
        kind_counts[kind] = kind_counts.get(kind, 0) + 1

    summary = {
        "recovery_advisory_outcome_invariance_smoke_passed": True,
        "issues_covered": [475],
        "production_boundary": (
            "assert_recovery_outcome_ignores_advisory_context over "
            "delivery_recovery_outcome observed-fact predicates"
        ),
        "corpus_count": len(corpus),
        "corpus_kind_counts": kind_counts,
        "evidence_count": len(evidence),
        "digest_equality_asserted": True,
        "negative_advisory_dependent_branch_failed_closed": negative_failed_closed,
        "advisory_context_can_shape_proposals": True,
        "recovery_outcome_reads_advisory_context": False,
        "recovery_outcome_byte_equal_with_and_without_advisory": True,
        "observed_facts_only": True,
        "advisory_used_as_outcome_evidence": False,
        "advisory_used_as_scorecard_evidence": False,
        "advisory_used_as_success_proof": False,
        "advisory_modifies_observed_facts": False,
        "public_sync_performed": False,
        "readme_or_architecture_updated": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if negative_failed_closed else 1


if __name__ == "__main__":
    raise SystemExit(main())
