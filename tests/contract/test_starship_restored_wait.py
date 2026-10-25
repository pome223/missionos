"""Injected clocks only; pacing never claims model or plant execution."""
import pytest

from src.runtime.starship_restored_wait import RestoredPendingPacer, RestoredWaitEpoch


class Clock:
    def __init__(self):
        self.now, self.sleeps = 100., []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= .05
        self.sleeps.append(seconds)
        self.now += seconds


def pacer(clock):
    return RestoredPendingPacer(RestoredWaitEpoch(2000., 100., 2035., 135., 2.),
        clock=clock, sleep=clock.sleep)


def test_fast_external_answer_does_not_assign_or_skip_actual_coast():
    clock = Clock()
    wait = pacer(clock)
    observed = []
    row = wait.at_actual_tick(2002., pump=lambda: observed.append(clock.now))
    assert clock.now == 102. and observed[0] == 100.
    assert not row["pending_deadline_expired"]
    assert not row["simulation_clock_assigned"] and not row["plant_steps_skipped"]
    assert wait.record()["actual_tick_observations"] == 1


def test_slow_compute_consumes_the_original_window_without_sleep_or_reset():
    clock = Clock()
    wait = pacer(clock)
    clock.now = 136.
    row = wait.at_actual_tick(2002.)
    assert row["pending_deadline_expired"] and not clock.sleeps
    assert wait.record()["wall_deadline_s"] == 135.


def test_final_coast_tick_can_cross_deadline_but_cannot_extend_it():
    clock = Clock()
    wait = pacer(clock)
    for now in range(2002, 2037, 2):
        row = wait.at_actual_tick(float(now))
    assert row["pending_deadline_expired"] and clock.now == 135.
    assert not row["original_deadline_extended"]


@pytest.mark.parametrize("bad", [1999., 2003., float("nan"), True])
def test_unobserved_clock_jump_or_regression_rejected(bad):
    clock = Clock()
    with pytest.raises(ValueError, match="tick_skipped_or_regressed"):
        pacer(clock).at_actual_tick(bad)
    assert not clock.sleeps


def test_wall_regression_rejected():
    clock = Clock()
    wait = pacer(clock)
    clock.now = 99.
    with pytest.raises(ValueError, match="wall_clock_regressed"):
        wait.at_actual_tick(2000.)
