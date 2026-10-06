"""Read-only entry timing diagnostics from saved, unthinned ship samples.

This separates an observed time offset from the sampled descent shape. Neither
linear interpolation nor a fitted offset authenticates the data or identifies
the physical/operational cause. The frozen flight model is never changed.
"""

from __future__ import annotations

from bisect import bisect_left
from hashlib import sha256
import json
import math
from statistics import median


ENTRY_IDS = tuple(f"V{i:02}" for i in range(17, 26))
FIT_IDS = tuple(f"V{i:02}" for i in range(20, 26))


def _hash(value: object) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _ship_events(run: dict, name: str) -> list[dict]:
    matches = []
    events = run.get("events", [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ValueError("run.events must be a list of event objects")
    for event in events:
        if event.get("event") == name and event.get("body") == "ship":
            matches.append({"time_s": _number(event.get("time_s"), f"{name}.time_s")})
    return matches


def _crossings(trace: list[dict], altitude: float, entry_time: float) -> list[dict]:
    """Return all descending linear crossings; a level interval is ambiguous.

    Shared sample endpoints are one crossing, not two. A plateau at the target
    altitude prevents selection of a unique crossing, including its endpoints.
    """
    candidates = []
    for index, (left, right) in enumerate(zip(trace, trace[1:])):
        t0, t1 = left["time_s"], right["time_s"]
        h0, h1 = left["altitude_m"], right["altitude_m"]
        if t1 < entry_time:
            continue
        bracket = {
            "sample_indices": [index, index + 1],
            "bracket_s": [t0, t1],
            "bracket_altitude_m": [h0, h1],
            "bracket_width_s": t1 - t0,
        }
        if h0 == h1 == altitude:
            candidates.append(
                {
                    **bracket,
                    "kind": "level_interval",
                    "time_s": None,
                    "interval_s": [max(entry_time, t0), t1],
                    "interpolation_fraction": None,
                }
            )
        elif h0 > h1 and h0 >= altitude >= h1:
            fraction = (h0 - altitude) / (h0 - h1)
            time = t0 + fraction * (t1 - t0)
            if time < entry_time:
                continue
            # Only exact shared endpoints are deduplicated, not nearby passes.
            if any(c["time_s"] == time for c in candidates):
                continue
            candidates.append(
                {
                    **bracket,
                    "kind": "descending_linear_crossing",
                    "time_s": time,
                    "interpolation_fraction": fraction,
                }
            )
    return candidates


def _altitude_comparison(trace: list[dict], observations: dict, offset: float | None) -> dict:
    """A full-coverage-only time-sampled summary, always retaining each bracket."""
    times = [sample["time_s"] for sample in trace]
    rows = []
    for identity in ENTRY_IDS:
        observation = observations.get(identity)
        time = (
            observation["time_s"] + offset
            if observation is not None and offset is not None
            else None
        )
        row = {
            "id": identity,
            "status": "missing_reference",
            "evaluation_time_s": time,
            "model_altitude_m": None,
            "altitude_difference_m": None,
            "bracket_s": None,
            "interpolation_fraction": None,
        }
        if observation is not None:
            row["status"] = "alignment_unavailable" if offset is None else "outside_trace"
            if time is not None and times and times[0] <= time <= times[-1]:
                right = bisect_left(times, time)
                left = max(0, right - 1)
                fraction = (
                    (time - times[left]) / (times[right] - times[left]) if right != left else 0.0
                )
                altitude = (
                    trace[left]["altitude_m"] * (1 - fraction)
                    + trace[right]["altitude_m"] * fraction
                )
                row.update(
                    status="covered",
                    model_altitude_m=altitude,
                    altitude_difference_m=altitude - observation["altitude_m"],
                    bracket_s=[times[left], times[right]],
                    interpolation_fraction=fraction,
                )
        rows.append(row)
    count = sum(row["status"] == "covered" for row in rows)
    return {
        "rows": rows,
        "reference_points": len(ENTRY_IDS),
        "covered_points": count,
        "full_coverage": count == len(ENTRY_IDS),
        "mean_absolute_difference_m": sum(abs(row["altitude_difference_m"]) for row in rows)
        / len(ENTRY_IDS)
        if count == len(ENTRY_IDS)
        else None,
    }


def diagnose_entry(run: dict, reference: dict) -> dict:
    """Diagnose V17..V25 without simulation, extrapolation, or altered inputs.

    Missing coverage has null metrics. V20..V25 are explicitly post-hoc fit
    points, not a validation set. Malformed or nonfinite selected trace/reference
    data raises ValueError; lack of observations/events is an explicit status.
    """
    if not isinstance(run, dict) or not isinstance(reference, dict):
        raise ValueError("run and reference must be objects")
    if not isinstance(run.get("traces"), dict):
        raise ValueError("run.traces must be an object")
    raw_trace = run["traces"].get("ship")
    if not isinstance(raw_trace, list):
        raise ValueError("run.traces.ship must be a list of saved samples")
    trace = []
    for index, sample in enumerate(raw_trace):
        if not isinstance(sample, dict):
            raise ValueError("ship samples must be objects")
        row = {
            key: _number(sample.get(key), f"ship[{index}].{key}")
            for key in ("time_s", "altitude_m")
        }
        if trace and row["time_s"] <= trace[-1]["time_s"]:
            raise ValueError("ship sample times must be strictly increasing")
        trace.append(row)
    observations = {}
    raw_observations = reference.get("observations", [])
    if not isinstance(raw_observations, list) or any(
        not isinstance(row, dict) for row in raw_observations
    ):
        raise ValueError("reference.observations must be a list of observation objects")
    for observation in raw_observations:
        identity = observation.get("id")
        if identity not in ENTRY_IDS:
            continue
        if identity in observations:
            raise ValueError(f"duplicate reference observation {identity}")
        observations[identity] = {
            "time_s": _number(observation.get("time_s"), f"{identity}.time_s"),
            "altitude_m": _number(observation.get("altitude_m"), f"{identity}.altitude_m"),
        }
    selected = [observations[i] for i in ENTRY_IDS if i in observations]
    if any(
        right["time_s"] <= left["time_s"] or right["altitude_m"] >= left["altitude_m"]
        for left, right in zip(selected, selected[1:])
    ):
        raise ValueError(
            "selected reference points must have increasing times and descending heights"
        )
    entry_events = _ship_events(run, "entry_interface")
    contact_events = _ship_events(run, "surface_contact")
    entry_status = (
        "available"
        if len(entry_events) == 1
        else ("missing_entry_interface" if not entry_events else "ambiguous_entry_interface")
    )
    entry_time = entry_events[0]["time_s"] if len(entry_events) == 1 else None
    if entry_time is not None and (
        not trace or not trace[0]["time_s"] <= entry_time <= trace[-1]["time_s"]
    ):
        entry_status = "entry_interface_outside_trace"
    rows = []
    for identity in ENTRY_IDS:
        observation = observations.get(identity)
        row = {
            "id": identity,
            "observed_time_s": observation["time_s"] if observation else None,
            "altitude_m": observation["altitude_m"] if observation else None,
            "alignment_fit_point": identity in FIT_IDS,
            "status": "missing_reference",
            "model_time_s": None,
            "delay_s": None,
            "residual_after_offset_s": None,
            "crossings": [],
        }
        if observation is not None:
            if entry_status != "available":
                row["status"] = entry_status
            else:
                row["crossings"] = _crossings(trace, observation["altitude_m"], entry_time)
                count = len(row["crossings"])
                if count == 1 and row["crossings"][0]["kind"] == "descending_linear_crossing":
                    row["status"] = "covered"
                    row["model_time_s"] = row["crossings"][0]["time_s"]
                    row["delay_s"] = row["model_time_s"] - observation["time_s"]
                else:
                    row["status"] = "ambiguous_crossing" if count else "missing_descending_crossing"
        rows.append(row)
    by_id = {row["id"]: row for row in rows}
    fit = [by_id[i] for i in FIT_IDS if by_id[i]["status"] == "covered"]
    full_fit = len(fit) == len(FIT_IDS)
    fit_chronological = (
        all(right["model_time_s"] > left["model_time_s"] for left, right in zip(fit, fit[1:]))
        if full_fit
        else None
    )
    alignment_available = full_fit and fit_chronological is True
    offset = median(row["delay_s"] for row in fit) if alignment_available else None
    if offset is not None:
        for row in rows:
            if row["delay_s"] is not None:
                row["residual_after_offset_s"] = row["delay_s"] - offset
    segments = []
    for first, last in (("V17", "V19"), ("V20", "V25")):
        left, right = by_id[first], by_id[last]
        covered = left["status"] == right["status"] == "covered"
        chronological = right["model_time_s"] > left["model_time_s"] if covered else None
        usable = covered and chronological is True
        observed = (
            right["observed_time_s"] - left["observed_time_s"]
            if left["observed_time_s"] is not None and right["observed_time_s"] is not None
            else None
        )
        modeled = right["model_time_s"] - left["model_time_s"] if usable else None
        segments.append(
            {
                "start_id": first,
                "end_id": last,
                "status": "covered"
                if usable
                else ("nonmonotonic_crossings" if covered else "missing_or_ambiguous_endpoint"),
                "endpoint_coverage": covered,
                "crossings_chronological": chronological,
                "observed_elapsed_s": observed,
                "model_elapsed_s": modeled,
                "elapsed_difference_s": modeled - observed if usable else None,
                "model_to_observed_duration_ratio": modeled / observed if usable else None,
            }
        )
    return {
        "schema": "missionos.starship_entry_diagnostic.v1",
        "scenario": run.get("scenario"),
        "method": "linear interpolation between stored ship sample heights after entry_interface",
        "reference_ids": list(ENTRY_IDS),
        "reference_points": len(ENTRY_IDS),
        "covered_points": sum(row["status"] == "covered" for row in rows),
        "missing_or_ambiguous_ids": [row["id"] for row in rows if row["status"] != "covered"],
        "rows": rows,
        "entry_interface": {"status": entry_status, "time_s": entry_time, "events": entry_events},
        "lower_segment_alignment": {
            "fit_ids": list(FIT_IDS),
            "fit_covered_points": len(fit),
            "full_coverage": full_fit,
            "crossings_chronological": fit_chronological,
            "alignment_available": alignment_available,
            "status": "available"
            if alignment_available
            else ("nonmonotonic_crossings" if full_fit else "missing_or_ambiguous_fit_point"),
            "median_offset_s": offset,
            "fit_delay_range_s": [
                min(row["delay_s"] for row in fit),
                max(row["delay_s"] for row in fit),
            ]
            if alignment_available
            else None,
            "max_abs_fit_residual_s": max(abs(row["residual_after_offset_s"]) for row in fit)
            if alignment_available
            else None,
            "scope": "post-hoc diagnostic alignment; these six points are fit points, not holdout validation",
        },
        "segments": segments,
        "same_time_altitude_diagnostic": {
            "original": _altitude_comparison(trace, observations, 0.0),
            "offset_aligned": _altitude_comparison(trace, observations, offset),
            "offset_s": offset,
            "scope": "post-hoc time shift fitted to V20-V25; not independent validation or a trajectory correction",
        },
        "surface_contact": {
            "model_events": contact_events,
            "observed_splashdown_time_s": None,
            "note": "V25 is a displayed altitude reading, not an observed surface-contact timestamp",
        },
        "trace_summary": {
            "sample_count": len(trace),
            "time_range_s": [trace[0]["time_s"], trace[-1]["time_s"]] if trace else None,
            "maximum_sample_gap_s": max(
                (b["time_s"] - a["time_s"] for a, b in zip(trace, trace[1:])), default=None
            ),
        },
        "provenance": {
            "canonical_selected_input_sha256": _hash(
                {
                    "scenario": run.get("scenario"),
                    "ship_time_altitude_samples": trace,
                    "entry_interface_events": entry_events,
                    "surface_contact_events": contact_events,
                }
            ),
            "canonical_reference_sha256": _hash(reference),
            "hash_scope": "canonical selected time/altitude samples and relevant event times; full supplied reference",
            "source_url": reference.get("source_url"),
            "altitude_basis": reference.get("altitude_basis"),
            "input_authentication_performed": False,
            "stored_verification_is_historical_only": True,
        },
        "trajectory_modified": False,
        "accuracy_verified": False,
        "operational_cause_verified": False,
        "limitations": [
            "Manual broadcast readings have unknown measurement uncertainty and altitude datum.",
            "Interpolation describes saved samples; sparse samples do not establish unsampled motion or event timing accuracy.",
            "Offset removal does not prove a return-decision timing error or aerodynamic correctness.",
            "Upper-entry points do not uniquely identify lift, drag, mass, or entry-angle parameters.",
            "Same-time altitude differences remain a separate diagnostic; this is not a replacement accuracy score.",
        ],
    }
