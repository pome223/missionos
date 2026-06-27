#!/usr/bin/env python3
"""Runtime smoke for advisory lesson verifier invariance (#449)."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import tempfile

from src.runtime.advisory_lesson_invariance import (
    assert_verifier_ignores_lessons,
    validate_verifier_contract_ref_is_current,
)
from src.runtime.advisory_mission_memory import current_verifier_contract
from tests.test_advisory_lesson_invariance import (
    _load_corpus,
    _promoted_lesson_registry,
    _run_verifier_case,
)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        corpus = _load_corpus()
        evidence = assert_verifier_ignores_lessons(
            corpus=corpus,
            verifier_runner=_run_verifier_case,
            full_lesson_registry=_promoted_lesson_registry(Path(tmp)),
        )
        contract = current_verifier_contract()
        validate_verifier_contract_ref_is_current(
            f"verifier_contract:{contract.contract_id}"
        )
        summary = {
            "verifier_invariance_runtime_smoke_passed": True,
            "production_boundary": (
                "fixed JSON verifier corpus through delivery review, "
                "logic recovery outcome, real-SITL recovery, and SITL dropoff "
                "verifier builders"
            ),
            "corpus_case_count": len(corpus),
            "evidence_case_count": len(evidence),
            "case_kinds": sorted({case["kind"] for case in corpus}),
            "case_counts_by_kind": dict(
                sorted(Counter(case["kind"] for case in corpus).items())
            ),
            "real_sitl_recovery_case_count": sum(
                1 for case in corpus if case["kind"] == "recovery_real_sitl"
            ),
            "verifier_contract_ref": f"verifier_contract:{contract.contract_id}",
            "current_contract_ref_validated": True,
            "public_sync_performed": False,
            "readme_or_architecture_updated": False,
            "issue_456_follow_up": True,
            "epic_442_close_allowed_after_merge": True,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if len(evidence) == len(corpus) >= 20 else 1


if __name__ == "__main__":
    raise SystemExit(main())
