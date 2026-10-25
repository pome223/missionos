"""Bounded deorbit-time screening toward pre-approved areas.

Three ordinary numerical forecasts suggest one time. This is not permission to
return: the suggested time still needs all four observation/refinement trials
and fresh independent admission. Areas, deadlines and plant state never move.
"""

from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
import math
import time

from .starship_replanning import digest, notices
from .starship_replanning_verifier import metrics, site_distance
from .starship_orbital_window import reserved_decision_s

CONFIG = {
    "policy": "bounded_quadratic_screen_v1",
    "center": "observed_next_orbit",
    "half_width_s": 120.0,
    "coarse_offsets_s": [-120.0, 0.0, 120.0],
    "maximum_screen_forecasts": 3,
    "refined_candidates": 1,
    "admission_trials": 4,
}


def suggest_time(rows, center, areas, notice=None):
    """One deterministic tool proposal from sampled distance, no outcome truth."""
    if len(rows) != 3 or any(metrics(t) is None for t in rows) or not areas:
        return None
    offsets = CONFIG["coarse_offsets_s"]
    if any(
        t["return_time_s"] != center + off or t["integration_scale"] != 1.0
        for t, off in zip(rows, offsets)
    ):
        raise ValueError("invalid_return_search_grid")
    proposals = []
    h = CONFIG["half_width_s"]
    for name, area in sorted(areas.items()):
        if notice is not None and (name not in notice["areas"] or not all(
                notice["areas"][name][0] <= metrics(t)["time_s"] <= min(notice["areas"][name][1], notice["expires_at_s"])
                for t in rows)):
            continue
        y = [site_distance(metrics(t), area) ** 2 for t in rows]
        curvature = (y[2] + y[0] - 2 * y[1]) / (2 * h * h)
        slope = (y[2] - y[0]) / (2 * h)
        off = (
            max(-h, min(h, -slope / (2 * curvature))) if curvature > 0 else offsets[y.index(min(y))]
        )
        predicted = y[1] + slope * off + curvature * off * off
        if not all(math.isfinite(x) for x in (*y, off, predicted)):
            raise ValueError("nonfinite_return_search")
        proposals.append((predicted / area["radius_m"] ** 2, name, center + off))
    if not proposals:
        return None
    _, area, when = min(proposals)
    return {
        "return_time_s": when,
        "screen_target_area": area,
        "method": "quadratic_distance_screen_not_admission",
    }


def search(actor, *, requested_names=("next_orbit",), cutoff_time_s=None, budget_s=None):
    """At most three forecasts, once per handoff; preserve any valid booking."""
    from .starship_replanning_executor import prediction_job

    if getattr(actor, "return_time_search", None) is not None:
        return actor.return_time_search["accepted_for_trial"]
    center = actor.times["next_orbit"]
    origin = actor.observation()
    notice = notices(actor.case, actor.s.time_s)
    row = {
        "configuration": deepcopy(CONFIG),
        "requested_names": list(requested_names),
        "start_time_s": actor.s.time_s,
        "center_s": center,
        "origin": origin,
        "notice": notice,
        "source_sha256": deepcopy(actor.sources),
        "forecast_count_before": actor.forecast_count,
        "deployment_binding": deepcopy(actor.deployment_binding),
        "trials": [],
        "suggestion": None,
        "accepted_for_trial": False,
        "return_admission_granted": False,
    }
    actor.return_time_search = row
    # Never let optional screening threaten an already checked booking.
    lead = (actor.envelope["batch_timeout_s"] + reserved_decision_s(actor.envelope)
            + actor.envelope["event_supervision"]["deadline_margin_s"])
    cutoff = min(center-CONFIG["half_width_s"]-lead,
                 actor.scheduled+actor.envelope["maximum_delay_s"],
                 cutoff_time_s if cutoff_time_s is not None else float("inf"))
    if "nominal" in requested_names:
        cutoff = min(cutoff, actor.times["nominal"]-lead)
    limit = min(actor.envelope["batch_timeout_s"], budget_s if budget_s is not None else float("inf"))
    bounded = budget_s is not None and bool(actor.envelope.get("comparison_preparation"))
    coast_dt = actor.p["integration"]["coast_dt_s"]
    if bounded:
        # Screening and independent admission share one preparation deadline.
        # Allocate by forecast count, while preserving the existing minimum
        # admission window. A short screen returns its unused time to admission.
        preparation_cutoff = min(actor.s.time_s+limit,
                                 cutoff_time_s if cutoff_time_s is not None else float("inf"))
        # Subtraction at fractional plant timestamps can round one ulp above
        # the caller's budget. Never let that arithmetic expand its hard cap.
        total = max(0.0, min(limit, preparation_cutoff-actor.s.time_s))
        minimum = actor.envelope["comparison_preparation"].get("minimum_start_budget_s", coast_dt)
        trials = CONFIG["admission_trials"]*len(requested_names)
        reserved = min(total, max(minimum, total*trials/(CONFIG["maximum_screen_forecasts"]+trials)))
        limit = total-reserved
        row["budget"] = {
            "total_budget_s": total,
            "preparation_cutoff_time_s": preparation_cutoff,
            "minimum_admission_budget_s": minimum,
            "reserved_admission_budget_s": reserved,
            "screen_budget_s": limit,
        }
        cutoff = min(cutoff, actor.s.time_s+limit)
    row.update(cutoff_time_s=cutoff, reserved_trial_count=4*len(requested_names), decision_lead_s=lead)
    enough_time = (actor.s.time_s+coast_dt < cutoff if bounded
                   else actor.s.time_s+limit < cutoff)
    allowed = (
        actor.selected is None
        and actor.deployment["authority"]["return_time_search"] == CONFIG
        and actor.forecast_count + 3 + 4*len(requested_names) <= actor.envelope["maximum_forecasts"]
        and enough_time
        and actor.scheduled <= center - 120 < center + 120 <= actor.scheduled + 6000
    )
    pool = None
    futures = []
    operation_index = len(actor.pending) if allowed and hasattr(actor, "pending") else None
    if operation_index is not None:
        row["pending_operation_index"] = operation_index
    started = time.monotonic() if allowed else None
    row["work_started"] = allowed
    if not allowed:
        row["reason"] = "search_budget_booking_or_deadline"
    else:
        jobs = [
            (
                origin,
                actor.p,
                center + off,
                actor.scheduled,
                str(actor.output / f"forecast-screen-{actor.forecast_count + i}.json"),
                1.0,
            ) + ((True,) if getattr(actor, "compact_artifacts", False) else ())
            for i, off in enumerate(CONFIG["coarse_offsets_s"])
        ]
        actor.forecast_count += len(jobs)
        try:
            pool = ProcessPoolExecutor(max_workers=actor.envelope["maximum_workers"])
            futures = [pool.submit(prediction_job, job) for job in jobs]

            def poll():
                return [f.result() for f in futures] if all(f.done() for f in futures) else None

            remaining = max(0.0, limit-(time.monotonic()-started))
            rows = actor.wait(
                "return_forecasts", poll, remaining, cutoff_time_s=cutoff
            )
            row["trials"] = [] if rows is None else rows
            row["reason"] = "screen_incomplete"
            if rows is not None:
                proposal = suggest_time(rows, center, actor.envelope["operations"]["areas"], notice)
                row["suggestion"] = proposal
                row["reason"] = (
                    "screen_no_finite_contact"
                    if proposal is None
                    else "screen_complete_requires_admission"
                )
        except Exception as exc:
            # Optional tool failure must not erase flight state or other choices.
            row.update(reason="screen_failed", error_type=type(exc).__name__)
        finally:
            if pool is not None:
                try:
                    if any(not f.done() for f in futures):
                        for proc in (getattr(pool, "_processes", None) or {}).values():
                            proc.terminate()
                    pool.shutdown(wait=True, cancel_futures=True)
                except Exception as exc:
                    row.update(reason="screen_shutdown_failed", shutdown_error_type=type(exc).__name__)
    # Cleanup cannot freeze physical time at the optional search cutoff.
    # Catch up within the already approved coast horizon, then discard stale
    # search results. Only an overrun of that mission horizon is unresolved.
    clock_horizon = actor.scheduled + actor.envelope["maximum_delay_s"]
    while (started is not None and actor.s.time_s-row["start_time_s"] < time.monotonic()-started
           and actor.s.time_s+actor.p["integration"]["coast_dt_s"] <= clock_horizon):
        actor.step()
    elapsed = 0.0 if started is None else time.monotonic()-started
    row["clock_debt_s"] = max(0.0, elapsed-(actor.s.time_s-row["start_time_s"]))
    row["clock_budget_exhausted"] = row["clock_debt_s"] > 1e-6
    row["search_cutoff_exceeded"] = actor.s.time_s >= cutoff
    row["clock_horizon_s"] = clock_horizon
    proposal = row["suggestion"]
    if (proposal is not None and elapsed <= limit and not row["search_cutoff_exceeded"] and not row["clock_budget_exhausted"] and "shutdown_error_type" not in row
            and actor.s.time_s < min(center, proposal["return_time_s"])-lead
            and notices(actor.case, actor.s.time_s) == notice):
        actor.times["next_orbit"] = proposal["return_time_s"]
        actor.monitor.times["next_orbit"] = proposal["return_time_s"]
        row["accepted_for_trial"] = True
    row.update(
        end_time_s=actor.s.time_s,
        wall_time_s=elapsed,
        forecast_count_after=actor.forecast_count,
        later_observation=actor.observation(),
    )
    if operation_index is not None:
        if len(actor.pending) == operation_index:
            actor.pending.append({"operation": "return_forecasts", "expired": True})
        operation = actor.pending[operation_index]
        operation.update(
            start_time_s=row["start_time_s"], end_time_s=row["end_time_s"],
            wall_time_s=elapsed, cutoff_time_s=cutoff,
            cutoff_reached=bool(operation.get("cutoff_reached") or row["search_cutoff_exceeded"]),
            expired=bool(operation.get("expired") or not row["accepted_for_trial"]),
        )
        if "error_type" in row:
            operation["error_type"] = row["error_type"]
    actor.event("m2_return_time_screen", reason=row["reason"], record_sha256=digest(row))
    return row["accepted_for_trial"]
