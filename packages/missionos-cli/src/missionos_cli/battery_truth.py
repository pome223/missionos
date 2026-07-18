"""Shared read-only battery truth projection for CLI surfaces."""

from __future__ import annotations

from typing import Any


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return "" if value in (None, "") else str(value)


def battery_truth_model(
    *,
    snapshot: dict[str, Any],
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the last trusted battery sample without accepting a reset jump.

    PX4/Gazebo may restart its reported battery percentage during teardown or a
    simulator process transition. All CLI surfaces must therefore use the same
    accepted live-trajectory sample rather than presenting the latest raw
    snapshot as continuous battery truth.
    """

    trajectory = artifacts.get("missionos_auto_mission_live_trajectory")
    trajectory = trajectory if isinstance(trajectory, dict) else {}
    samples = trajectory.get("samples")
    samples = samples if isinstance(samples, list) else []

    trusted_percent: float | None = None
    trusted_source = ""
    trusted_observed_at: Any = None
    trusted_sample_index: Any = None
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        percent = _float_or_none(sample.get("battery_remaining_percent"))
        source = _text(sample.get("battery_state_source"))
        if percent is None or sample.get("battery_sample_accepted") is False or not source:
            continue
        trusted_percent = percent
        trusted_source = source
        trusted_observed_at = sample.get("observed_at")
        trusted_sample_index = sample.get("sample_index")

    reported_percent = _float_or_none(snapshot.get("battery_remaining_percent"))
    reported_source = _text(snapshot.get("battery_state_source"))
    reported_observed_at = snapshot.get("observed_at")
    reported_sample_index = snapshot.get("sample_index")
    accepted = snapshot.get("battery_sample_accepted") is not False
    reset_delta_percent = (
        reported_percent - trusted_percent
        if reported_percent is not None and trusted_percent is not None
        else None
    )
    reset_detected = bool(reset_delta_percent is not None and reset_delta_percent > 2.0)
    source_missing = reported_percent is not None and not reported_source
    current_trusted = bool(
        reported_percent is not None
        and reported_source
        and accepted
        and not reset_detected
    )
    if current_trusted:
        trusted_percent = reported_percent
        trusted_source = reported_source
        trusted_observed_at = reported_observed_at
        trusted_sample_index = reported_sample_index

    display_percent = trusted_percent if trusted_percent is not None else reported_percent
    status = (
        "suspect_reset"
        if reset_detected
        else "sample_rejected"
        if not accepted and reported_percent is not None
        else "source_missing"
        if source_missing
        else "observed"
        if display_percent is not None
        else "unavailable"
    )
    return {
        "display_percent": display_percent,
        "reported_percent": reported_percent,
        "trusted_percent": trusted_percent,
        "source": trusted_source or reported_source or "unknown",
        "reported_source": reported_source or "unknown",
        "observed_at": trusted_observed_at or reported_observed_at,
        "sample_index": trusted_sample_index or reported_sample_index,
        "status": status,
        "reset_detected": reset_detected,
        "reset_delta_percent": reset_delta_percent,
        "sample_accepted": accepted,
        "warning": snapshot.get("battery_warning"),
    }
