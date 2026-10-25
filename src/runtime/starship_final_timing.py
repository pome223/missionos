"""Suspend-inclusive mission budget accounting; no execution or authority.

UTC elapsed time catches host sleep on platforms whose monotonic clock pauses.
The larger clock delta is enforced. UTC regression is refused conservatively;
clock divergence is recorded, never asserted to be a measurement of sleep.
"""
from copy import deepcopy
import math
import time


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def elapsed(start, end, maximum_s):
    if not (_finite(maximum_s) and maximum_s > 0 and type(start) is dict
            and type(end) is dict and set(start) == set(end) == {"monotonic_s", "utc_s"}
            and all(_finite(x) for stamp in (start, end) for x in stamp.values())):
        raise ValueError("mission_timing_finite_clock_pair_required")
    mono, utc = (end[k]-start[k] for k in ("monotonic_s", "utc_s"))
    if not _finite(mono) or not _finite(utc):
        raise ValueError("mission_timing_finite_elapsed_required")
    if min(mono, utc) < 0:
        raise ValueError("mission_timing_clock_regressed")
    return {"monotonic_elapsed_s": mono, "utc_elapsed_s": utc,
        "enforced_elapsed_s": max(mono, utc), "clock_divergence_s": utc-mono,
        "maximum_s": maximum_s, "within_budget": max(mono, utc) < maximum_s}


class MissionWallBudget:
    def __init__(self, maximum_s, *, clock=None, wall_clock=None, reason="mission_wall_budget_exceeded"):
        if not _finite(maximum_s) or maximum_s <= 0:
            raise ValueError("mission_timing_positive_budget_required")
        self.clock = time.monotonic if clock is None else clock
        self.wall_clock = time.time if wall_clock is None else wall_clock
        self.maximum_s, self.reason = maximum_s, reason
        self.start = self.last = self._sample()
        self.checks, self.closed, self.status, self.phase = 0, False, "running", "start"

    def _sample(self):
        stamp = {"monotonic_s": self.clock(), "utc_s": self.wall_clock()}
        if not all(_finite(v) for v in stamp.values()):
            raise ValueError("mission_timing_finite_clock_pair_required")
        return stamp

    def check(self, phase="progress"):
        if self.closed:
            raise ValueError("mission_timing_already_closed")
        self.checks += 1
        self.phase = phase
        try:
            now = self._sample()
            elapsed(self.last, now, self.maximum_s)
        except ValueError:
            self.status = "invalid_clock"
            raise
        self.last = now
        result = elapsed(self.start, now, self.maximum_s)
        if not result["within_budget"]:
            self.status = "exceeded"
            raise ValueError(self.reason)
        return result

    def finish(self):
        try:
            result = self.check("complete")
            self.status = "completed"
            return result
        finally:
            self.closed = True

    def record(self):
        return {"schema": "missionos.starship_final_wall_timing.v1",
            "clock_policy": "enforce_maximum_utc_and_monotonic_elapsed_refuse_regression",
            "start": deepcopy(self.start), "end": deepcopy(self.last),
            **elapsed(self.start, self.last, self.maximum_s),
            "checks": self.checks, "closed": self.closed, "status": self.status,
            "phase": self.phase, "sleep_duration_inferred_from_clock_difference": False}


def verify_completed(record, *, maximum_s):
    if type(record) is not dict or record.get("schema") != "missionos.starship_final_wall_timing.v1":
        raise ValueError("mission_timing_record_schema")
    calculated = elapsed(record["start"], record["end"], maximum_s)
    if not (all(record.get(k) == v for k, v in calculated.items())
            and record.get("closed") is True and record.get("status") == "completed"
            and record.get("phase") == "complete" and type(record.get("checks")) is int
            and record["checks"] >= 1 and calculated["within_budget"] is True
            and record.get("clock_policy") == "enforce_maximum_utc_and_monotonic_elapsed_refuse_regression"
            and record.get("sleep_duration_inferred_from_clock_difference") is False):
        raise ValueError("mission_timing_not_completed_within_budget")
    return calculated
