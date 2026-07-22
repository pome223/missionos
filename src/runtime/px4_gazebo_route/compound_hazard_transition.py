"""Fail-closed evidence for wind-to-obstacle Recovery transitions."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


WIND_SAFE_WINDOW_SCHEMA_VERSION = "missionos_runtime_recovery_wind_safe_window.v1"


def _number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def build_wind_safe_window_evidence(
    samples: Sequence[Mapping[str, Any]],
    *,
    recovery_policy: Mapping[str, Any],
    minimum_window_s: float,
    maximum_sample_gap_s: float,
) -> dict[str, Any]:
    """Verify a bounded fresh-telemetry window below the recovery wind limit."""

    wind_limit_mps = _number(recovery_policy.get("max_wind_speed_mps"))
    minimum_window_s = max(0.001, float(minimum_window_s))
    maximum_sample_gap_s = max(0.001, float(maximum_sample_gap_s))
    normalized: list[tuple[float, Mapping[str, Any]]] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        elapsed_s = _number(sample.get("elapsed_seconds"))
        if elapsed_s is not None:
            normalized.append((elapsed_s, sample))
    normalized.sort(key=lambda item: item[0])
    latest_elapsed_s = normalized[-1][0] if normalized else None
    window_start_s = (
        latest_elapsed_s - minimum_window_s
        if latest_elapsed_s is not None
        else None
    )
    in_window = [
        (elapsed_s, sample)
        for elapsed_s, sample in normalized
        if window_start_s is not None and elapsed_s > window_start_s
    ]
    predecessors = [
        (elapsed_s, sample)
        for elapsed_s, sample in normalized
        if window_start_s is not None and elapsed_s <= window_start_s
    ]
    window_samples = ([predecessors[-1]] if predecessors else []) + in_window
    observed_winds: list[float] = []
    missing_wind_samples = 0
    stale_samples = 0
    for _, sample in window_samples:
        wind = sample.get("wind")
        wind = wind if isinstance(wind, Mapping) else {}
        wind_speed_mps = _number(
            wind.get("speed_mps"),
            wind.get("observed_speed_mps"),
            sample.get("wind_speed_mps"),
        )
        if wind_speed_mps is None:
            missing_wind_samples += 1
        else:
            observed_winds.append(wind_speed_mps)
        telemetry = sample.get("telemetry")
        telemetry = telemetry if isinstance(telemetry, Mapping) else {}
        if telemetry.get("stale") is True:
            stale_samples += 1
    coverage_start_s = (
        window_start_s
        if predecessors and window_start_s is not None
        else window_samples[0][0]
        if window_samples
        else None
    )
    observed_span_s = (
        window_samples[-1][0] - coverage_start_s
        if window_samples and coverage_start_s is not None
        else 0.0
    )
    observed_gaps_s = [
        current[0] - previous[0]
        for previous, current in zip(window_samples, window_samples[1:])
    ]
    maximum_observed_gap_s = max(observed_gaps_s) if observed_gaps_s else None
    wind_max_mps = max(observed_winds) if observed_winds else None
    blocking_reasons: list[str] = []
    if wind_limit_mps is None:
        blocking_reasons.append("recovery_wind_limit_missing")
    if observed_span_s < minimum_window_s:
        blocking_reasons.append("wind_safe_window_duration_insufficient")
    if (
        maximum_observed_gap_s is not None
        and maximum_observed_gap_s > maximum_sample_gap_s
    ):
        blocking_reasons.append("wind_safe_window_sample_gap_exceeded")
    if missing_wind_samples or not observed_winds:
        blocking_reasons.append("wind_safe_window_observation_missing")
    if stale_samples:
        blocking_reasons.append("wind_safe_window_telemetry_stale")
    if (
        wind_limit_mps is not None
        and wind_max_mps is not None
        and wind_max_mps > wind_limit_mps
    ):
        blocking_reasons.append("wind_safe_window_limit_exceeded")
    verified = not blocking_reasons
    return {
        "schema_version": WIND_SAFE_WINDOW_SCHEMA_VERSION,
        "verification_status": "verified_safe" if verified else "unverified",
        "safe_window_observed": verified,
        "minimum_window_s": minimum_window_s,
        "maximum_sample_gap_s": maximum_sample_gap_s,
        "observed_window_s": round(observed_span_s, 3),
        "maximum_observed_sample_gap_s": (
            round(maximum_observed_gap_s, 3)
            if maximum_observed_gap_s is not None
            else None
        ),
        "sample_count": len(window_samples),
        "wind_observation_count": len(observed_winds),
        "wind_speed_max_mps": (
            round(wind_max_mps, 3) if wind_max_mps is not None else None
        ),
        "max_wind_speed_mps": wind_limit_mps,
        "telemetry_stale_count": stale_samples,
        "missing_wind_sample_count": missing_wind_samples,
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
        "dispatch_authority_created": False,
        "progress_counted": False,
        "physical_execution_invoked": False,
    }


def proposal_trigger_reasons(proposal: Mapping[str, Any]) -> set[str]:
    result = proposal.get("runtime_recovery_agent_result")
    result = result if isinstance(result, Mapping) else {}
    agent_output = result.get("agent_output")
    agent_output = agent_output if isinstance(agent_output, Mapping) else {}
    assessment = result.get("assessment")
    assessment = assessment if isinstance(assessment, Mapping) else {}
    reasons = [
        *list(agent_output.get("trigger_reasons") or []),
        *list(assessment.get("observed_risk_reasons") or []),
    ]
    return {str(reason).strip() for reason in reasons if str(reason).strip()}


__all__ = [
    "WIND_SAFE_WINDOW_SCHEMA_VERSION",
    "build_wind_safe_window_evidence",
    "proposal_trigger_reasons",
]
