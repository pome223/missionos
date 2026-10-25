"""Wall pacing for actual coast ticks during a bounded external wait.

This helper neither advances nor assigns plant state. The caller must execute
each existing finite step and pass its actual observed time. Pacing is needed
only while a tool/model/operator request is pending, so an accelerated simulator
does not spend the entire decision window before an external reply can arrive.
"""
from dataclasses import dataclass
import math
import time


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class RestoredWaitEpoch:
    simulation_start_s: float
    wall_start_s: float
    simulation_deadline_s: float
    wall_deadline_s: float
    maximum_existing_step_s: float

    def __post_init__(self):
        if (not all(_number(x) for x in (
                self.simulation_start_s, self.wall_start_s, self.simulation_deadline_s,
                self.wall_deadline_s, self.maximum_existing_step_s))
                or min(self.simulation_start_s, self.wall_start_s) < 0
                or not self.simulation_start_s < self.simulation_deadline_s
                or not self.wall_start_s < self.wall_deadline_s
                or not 0 < self.maximum_existing_step_s <= 2.01):
            raise ValueError("restored_wait_invalid_original_epoch")


class RestoredPendingPacer:
    """One original wait epoch, with short pump intervals and no clock reset."""

    def __init__(self, epoch, *, clock=time.monotonic, sleep=time.sleep):
        if type(epoch) is not RestoredWaitEpoch or not callable(clock) or not callable(sleep):
            raise ValueError("restored_wait_typed_epoch_required")
        self.epoch, self.clock, self.sleep = epoch, clock, sleep
        self._last_simulation = epoch.simulation_start_s
        self._last_wall = epoch.wall_start_s
        self._ticks = self._sleeps = 0

    def at_actual_tick(self, simulation_time_s, *, pump=lambda: None):
        """Pace a measured tick; pump may receive replies but cannot reset time.

        A reply observed before the tick's wall epoch is retained by the caller
        and applied only after this method returns. A deadline can expire while
        pacing; callers then choose their separately approved fallback.
        """
        epoch = self.epoch
        if (not _number(simulation_time_s) or not callable(pump)
                or not self._last_simulation <= simulation_time_s
                <= self._last_simulation+epoch.maximum_existing_step_s+1e-7
                or simulation_time_s > epoch.simulation_deadline_s+epoch.maximum_existing_step_s+1e-7):
            raise ValueError("restored_wait_actual_tick_skipped_or_regressed")
        self._last_simulation = simulation_time_s
        self._ticks += 1
        target = min(epoch.wall_deadline_s,
            epoch.wall_start_s+simulation_time_s-epoch.simulation_start_s)
        while True:
            now = self.clock()
            if not _number(now) or now < self._last_wall:
                raise ValueError("restored_wait_wall_clock_regressed")
            self._last_wall = now
            pump()
            if now >= target:
                break
            self.sleep(min(.05, target-now))
            self._sleeps += 1
        return {"pending_deadline_expired": now >= epoch.wall_deadline_s
                or simulation_time_s >= epoch.simulation_deadline_s,
            "simulation_time_s": simulation_time_s, "wall_time_s": now,
            "simulation_clock_assigned": False, "plant_steps_skipped": False,
            "original_deadline_extended": False}

    def record(self):
        return {"schema": "missionos.starship_restored_pending_pacing.v1",
            "simulation_start_s": self.epoch.simulation_start_s,
            "wall_start_s": self.epoch.wall_start_s,
            "simulation_deadline_s": self.epoch.simulation_deadline_s,
            "wall_deadline_s": self.epoch.wall_deadline_s,
            "last_actual_tick_s": self._last_simulation, "last_wall_s": self._last_wall,
            "actual_tick_observations": self._ticks, "short_waits": self._sleeps,
            "simulation_clock_assigned": False, "plant_execution_inferred": False,
            "native_inference_inferred": False}
