"""Resource guard tests only; no model, simulator flight or unrelated process."""

import json
import signal
import time

import pytest

from src.runtime.starship_runtime_budget import BudgetExceeded, RuntimeBudget, _tree_bytes

LIMITS = {
    "maximum_wall_time_s": 2400,
    "maximum_output_bytes": 1200000000,
    "minimum_free_bytes": 1073741824,
}


def guard(tmp_path, now, *, size=0, free=2000000000):
    return RuntimeBudget(
        tmp_path / "new",
        LIMITS,
        clock=lambda: now[0],
        tree_bytes=lambda _: size,
        free_bytes=lambda _: free,
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("wall", "maximum_wall_time"),
        ("size", "maximum_output_bytes"),
        ("free", "minimum_free_bytes"),
    ],
)
def test_limits_abort_without_becoming_recoverable_tool_error(tmp_path, change, reason):
    now = [0.0]
    budget = guard(
        tmp_path,
        now,
        size=1200000000 if change == "size" else 0,
        free=1073741823 if change == "free" else 2000000000,
    )
    budget.started = 0.0
    if change == "wall":
        now[0] = 2400.0
    with pytest.raises(BudgetExceeded) as failure:
        budget.check()
    assert not isinstance(failure.value, Exception)
    assert failure.value.record["reason"] == reason
    budget.failure_artifact()
    saved = json.loads((tmp_path / "new/runtime-budget-failure.json").read_text())
    assert (
        saved["retry_performed"] is False and saved["physical_outcome"] == "unresolved_interrupted"
    )


def test_resource_read_failure_aborts_instead_of_ignoring_budget(tmp_path):
    budget = guard(tmp_path, [0.0])
    budget.started = 0.0
    budget.tree_bytes = lambda _: (_ for _ in ()).throw(OSError("fixture"))
    with pytest.raises(BudgetExceeded, match="budget_observation_unavailable"):
        budget.check()


def test_success_leaves_no_timer_and_does_not_create_output(tmp_path):
    previous = signal.getsignal(signal.SIGALRM)
    with guard(tmp_path, [0.0]) as budget:
        assert signal.getitimer(signal.ITIMER_REAL)[1] == 5.0
        budget.check()
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == previous
    assert not (tmp_path / "new").exists()


def test_real_timer_interrupts_main_thread_sleep_and_restores_handler(tmp_path):
    previous = signal.getsignal(signal.SIGALRM)
    limits = dict(LIMITS, maximum_wall_time_s=0.02)
    budget = RuntimeBudget(
        tmp_path / "new",
        limits,
        interval_s=0.01,
        tree_bytes=lambda _: 0,
        free_bytes=lambda _: 2000000000,
    )
    started = time.monotonic()
    with pytest.raises(BudgetExceeded):
        with budget:
            time.sleep(1.0)
    assert time.monotonic() - started < 0.5
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == previous


def test_does_not_replace_an_existing_timer(tmp_path):
    signal.setitimer(signal.ITIMER_REAL, 60.0)
    try:
        with pytest.raises(ValueError, match="already_in_use"):
            with guard(tmp_path, [0.0]):
                pytest.fail("must not enter")
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 50.0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)


def test_tree_measurement_does_not_follow_external_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "other.txt").write_bytes(b"x" * 200)
    output = tmp_path / "output"
    output.mkdir()
    (output / "completed.json").write_bytes(b"12345")
    (output / "foreign").symlink_to(outside, target_is_directory=True)
    assert _tree_bytes(output) == 5
    now = [0.0]
    budget = guard(tmp_path, now)
    budget.output = output
    budget.started = 0.0
    now[0] = 2400.0
    with pytest.raises(BudgetExceeded):
        budget.check()
    budget.failure_artifact()
    assert (output / "completed.json").read_bytes() == b"12345"
    assert (outside / "other.txt").read_bytes() == b"x" * 200
