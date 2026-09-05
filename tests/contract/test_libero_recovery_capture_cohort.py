from __future__ import annotations

import argparse

import pytest

from scripts.run_libero_recovery_capture_cohort import (
    PREREGISTERED_CANDIDATES,
    PREREGISTERED_EVALUATION_HOLDOUTS,
    execute,
    parse_candidate,
)


def test_minimal_cohort_is_preregistered_and_bounded() -> None:
    assert PREREGISTERED_CANDIDATES == ((0, 101), (1, 102), (2, 103), (3, 104))
    assert PREREGISTERED_EVALUATION_HOLDOUTS == ((4, 0), (12, 0), (15, 0))
    assert parse_candidate("2:103") == (2, 103)
    assert parse_candidate("12:0") == (12, 0)
    with pytest.raises(argparse.ArgumentTypeError, match="outside"):
        parse_candidate("5:0")


def test_execute_requires_explicit_opt_in(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RUN_MISSIONOS_LIBERO_RECOVERY_CAPTURE_COHORT", raising=False)
    with pytest.raises(RuntimeError, match="opt_in_required"):
        execute(output_dir=tmp_path / "cohort", candidates=[(0, 101)])


def test_execute_rejects_duplicate_or_unregistered_candidates(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUN_MISSIONOS_LIBERO_RECOVERY_CAPTURE_COHORT", "1")
    with pytest.raises(ValueError, match="candidates_invalid"):
        execute(output_dir=tmp_path / "duplicates", candidates=[(0, 101), (0, 101)])
    with pytest.raises(ValueError, match="not_preregistered"):
        execute(output_dir=tmp_path / "outside", candidates=[(4, 0)], cohort="training")
    with pytest.raises(ValueError, match="not_preregistered"):
        execute(output_dir=tmp_path / "wrong_split", candidates=[(0, 101)], cohort="evaluation")
