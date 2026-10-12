"""Cooperative physical-clock scheduling of Ship and its separated booster.

The scheduler chooses the smallest pending physical time before any body runs
its observers or control. Records are appended in execution order, never sorted
after a sequential flight. Each body may lead the other by one integration tick.
Released satellites retain their existing later orbit-only propagation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math


@dataclass(frozen=True)
class PhysicsTick:
    body_id: str
    time_s: float
    phase: str
    state: object
    booster_separation_state: object = None
    sequencer_state: str | None = None
    released_count: int = 0
    deployment_withheld: bool = False

    def __post_init__(self):
        if (self.body_id not in ("ship", "booster") or type(self.time_s) not in (int, float)
                or not math.isfinite(self.time_s) or type(self.phase) is not str
                or getattr(self.state, "time_s", None) != self.time_s
                or type(self.released_count) is not int or self.released_count < 0
                or type(self.deployment_withheld) is not bool):
            raise ValueError("invalid_physics_tick")
        if self.booster_separation_state is not None and (self.body_id != "ship"
                or self.booster_separation_state.time_s != self.time_s):
            raise ValueError("invalid_separation_tick")


def drain(iterator):
    """Preserve the pre-generator synchronous return and exception behavior."""
    while True:
        try:
            next(iterator)
        except StopIteration as completed:
            return completed.value


def coordinate(ship, booster_factory, *, maximum_tick_s, on_tick=None):
    if type(maximum_tick_s) not in (int, float) or not math.isfinite(maximum_tick_s) or maximum_tick_s <= 0:
        raise ValueError("invalid_shared_clock_tick_bound")
    if on_tick is not None and not callable(on_tick):
        raise ValueError("invalid_tick_observer")
    generators, pending, results = {"ship": ship}, {}, {}
    trace, separation = [], None
    last_time, max_lead = -math.inf, 0.

    def pull(body):
        nonlocal separation
        try:
            tick = next(generators[body])
        except StopIteration as completed:
            results[body] = completed.value
            return
        if type(tick) is not PhysicsTick or tick.body_id != body:
            raise ValueError("unexpected_shared_clock_body_tick")
        pending[body] = tick
        if tick.booster_separation_state is not None:
            if separation is not None:
                raise ValueError("duplicate_shared_clock_separation")
            separation = tick.booster_separation_state
            generators["booster"] = booster_factory(separation)
            pull("booster")
            if "booster" not in pending or pending["booster"].time_s != tick.time_s:
                raise ValueError("booster_must_start_at_exact_separation_time")

    try:
        pull("ship")
        while pending:
            # A deterministic tie-break advances booster first at separation.
            body = min(pending, key=lambda key: (pending[key].time_s, key))
            tick = pending.pop(body)
            if tick.time_s < last_time-1e-9:
                raise ValueError("shared_clock_moved_backwards")
            last_time = tick.time_s
            if on_tick is not None:
                on_tick(tick)
            row = {"sequence": len(trace), "body_id": body, "time_s": tick.time_s, "phase": tick.phase}
            if body == "ship":
                row.update(sequencer_state=tick.sequencer_state, released_count=tick.released_count,
                           deployment_withheld=tick.deployment_withheld)
            trace.append(row)
            pull(body)
            end = (pending[body].time_s if body in pending else results[body]["final_state"]["time_s"])
            if not tick.time_s-1e-9 <= end <= tick.time_s+maximum_tick_s+1e-8:
                raise ValueError("body_exceeded_shared_clock_tick")
            row["next_time_s"] = end
            if len(pending) > 1:
                lead = max(t.time_s for t in pending.values())-min(t.time_s for t in pending.values())
                max_lead = max(max_lead, lead)
                if lead > maximum_tick_s+1e-8:
                    raise ValueError("shared_clock_body_lead_exceeded")
    except Exception as error:
        # Preserve executed tick order even when a plant/controller raises. This
        # is partial evidence, never a completed run or replayed final state.
        error.concurrent_execution = {"schema": "missionos.ship_booster_clock.v1",
            "tick_order": trace, "maximum_tick_s": maximum_tick_s,
            "maximum_observed_body_lead_s": max_lead, "incomplete": True,
            "posthoc_ordering": False, "physical_execution": False,
            "completed_bodies": list(results), "pending_bodies": list(pending)}
        raise
    finally:
        for generator in generators.values():
            generator.close()
    result = results["ship"]
    booster = results.get("booster")
    if separation is not None:
        def canonical(value):
            return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if (canonical(result.get("booster_separation_state")) != canonical(asdict(separation))
                or canonical(booster.get("booster_separation_state")) != canonical(asdict(separation))):
            raise ValueError("shared_clock_separation_state_changed")
    result["booster_run"] = booster
    result["outcome"]["booster_return_invoked"] = booster is not None
    result["concurrent_execution"] = {"schema": "missionos.ship_booster_clock.v1",
        "tick_order": trace, "maximum_tick_s": maximum_tick_s, "maximum_observed_body_lead_s": max_lead,
        "separation_state_preserved": separation is not None,
        "observers_run_before_each_body_step": True, "posthoc_ordering": False,
        "satellite_propagation": "existing_postprocessed_orbit_only", "physical_execution": False}
    return result
