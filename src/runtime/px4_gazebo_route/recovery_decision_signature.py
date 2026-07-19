"""Versioned semantic numeric deltas for PX4 recovery decisions.

The recovery agent already receives bounded telemetry windows.  This module
turns a small set of mission-level numeric facts into policy-relative bands so
that a materially worse situation can create a new LLM decision epoch without
making every telemetry refresh an LLM call.

The output is evidence only.  It never creates a proposal, approval, dispatch
authority, progress claim, or completion claim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
from typing import Any


SEMANTIC_NUMERIC_STATE_SCHEMA_VERSION = "missionos_runtime_recovery_semantic_numeric_state.v1"
SEMANTIC_NUMERIC_DELTA_SCHEMA_VERSION = "missionos_runtime_recovery_semantic_numeric_delta.v1"
RECOVERY_DECISION_SIGNATURE_VERSION = "missionos_runtime_recovery_decision_signature.v2"

_BANDS = (
    "below_watch",
    "near_limit",
    "limit_to_1_25x",
    "1_25x_to_1_5x",
    "above_1_5x",
)
_BAND_BOUNDARIES = (0.5, 1.0, 1.25, 1.5)
_DEFAULT_HYSTERESIS_FRACTION = 0.05
_DEFAULT_DEBOUNCE_OBSERVATIONS = 2
_MATERIAL_TREND_DELTA_RATIO = 0.15


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _band_index(name: Any) -> int | None:
    try:
        return _BANDS.index(str(name))
    except ValueError:
        return None


def _raw_band_index(risk_ratio: float) -> int:
    index = 0
    for boundary in _BAND_BOUNDARIES:
        if risk_ratio < boundary:
            break
        index += 1
    return min(index, len(_BANDS) - 1)


def _hysteresis_band_index(
    risk_ratio: float,
    *,
    prior_band: Any,
    hysteresis_fraction: float,
) -> int:
    raw_index = _raw_band_index(risk_ratio)
    prior_index = _band_index(prior_band)
    if prior_index is None or raw_index == prior_index:
        return raw_index
    if raw_index > prior_index:
        candidate = prior_index
        for boundary_index in range(prior_index, raw_index):
            boundary = _BAND_BOUNDARIES[boundary_index]
            if risk_ratio < boundary * (1.0 + hysteresis_fraction):
                break
            candidate += 1
        return candidate
    candidate = prior_index
    for boundary_index in range(prior_index - 1, raw_index - 1, -1):
        boundary = _BAND_BOUNDARIES[boundary_index]
        if risk_ratio >= boundary * (1.0 - hysteresis_fraction):
            break
        candidate -= 1
    return candidate


def _trailing_progress_stall_seconds(summary: Mapping[str, Any]) -> float | None:
    buckets = summary.get("buckets")
    if not isinstance(buckets, Sequence) or isinstance(buckets, (str, bytes)):
        return None
    stalled_seconds = 0.0
    observed = False
    for raw_bucket in reversed(buckets):
        bucket = _mapping(raw_bucket)
        if int(_number(bucket.get("sample_count")) or 0) <= 0:
            if observed:
                break
            continue
        progress_delta = _number(bucket.get("progress_delta_m"))
        if progress_delta is None or progress_delta > 0.0:
            break
        start = _number(bucket.get("elapsed_start_s"))
        end = _number(bucket.get("elapsed_end_s"))
        if start is None or end is None or end <= start:
            duration = _number(summary.get("bucket_s")) or 0.0
        else:
            duration = end - start
        stalled_seconds += max(0.0, duration)
        observed = True
    return stalled_seconds if observed else 0.0


def _battery_return_margin(telemetry_snapshot: Mapping[str, Any]) -> float | None:
    battery = _mapping(telemetry_snapshot.get("battery"))
    return_home = _mapping(battery.get("return_home_projection"))
    if str(return_home.get("projection_status") or "") != "computed":
        return None
    return _number(return_home.get("projected_return_reserve_margin_percent"))


def _semantic_observations(
    summary: Mapping[str, Any],
    *,
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    overall = _mapping(summary.get("overall"))
    thresholds = _mapping(summary.get("thresholds"))
    wind_limit = _number(recovery_policy.get("max_wind_speed_mps"))
    if wind_limit is None:
        wind_limit = _number(thresholds.get("wind_soft_limit_mps"))
    cross_track_limit = _number(thresholds.get("cross_track_soft_limit_m"))
    terrain_soft_margin = _number(thresholds.get("terrain_soft_margin_m"))
    terrain_minimum = _number(thresholds.get("min_terrain_clearance_m"))
    battery_margin_reference = _number(recovery_policy.get("battery_return_threshold_percent"))
    bucket_s = max(1.0, _number(summary.get("bucket_s")) or 5.0)
    progress_stall_threshold_s = bucket_s * 2.0
    telemetry_stale_threshold_count = 2.0

    terrain_margin = _number(overall.get("terrain_clearance_margin_min_m"))
    if terrain_margin is None:
        clearance = _number(overall.get("terrain_clearance_min_m"))
        if clearance is not None and terrain_minimum is not None:
            terrain_margin = clearance - terrain_minimum
    return_margin = _battery_return_margin(telemetry_snapshot)
    progress_stall_seconds = _trailing_progress_stall_seconds(summary)
    stale_count = _number(overall.get("trailing_telemetry_stale_count"))
    latest = _mapping(summary.get("latest"))
    # Historical stale samples remain in the audit window, but they are not a
    # current stale decision fact after the latest sample is fresh. This also
    # prevents host-side model latency from creating a self-exciting stale
    # signal while the in-container telemetry stream remains healthy.
    if latest.get("telemetry_stale") is False:
        stale_count = 0.0

    def _ratio(value: float | None, threshold: float | None) -> float | None:
        if value is None or threshold is None or threshold <= 0.0:
            return None
        return max(0.0, value / threshold)

    def _inverse_margin_ratio(
        margin: float | None,
        threshold: float | None,
    ) -> float | None:
        if margin is None or threshold is None or threshold <= 0.0:
            return None
        return max(0.0, 1.0 - (margin / threshold))

    return {
        "wind_margin_band": {
            "observed_value": _number(overall.get("wind_speed_max_mps")),
            "observed_unit": "m/s",
            "threshold_value": wind_limit,
            "threshold_ref": "recovery_policy:max_wind_speed_mps",
            "risk_ratio": _ratio(_number(overall.get("wind_speed_max_mps")), wind_limit),
        },
        "cross_track_margin_band": {
            "observed_value": _number(overall.get("cross_track_max_m")),
            "observed_unit": "m",
            "threshold_value": cross_track_limit,
            "threshold_ref": "recovery_window_summary:cross_track_soft_limit_m",
            "risk_ratio": _ratio(_number(overall.get("cross_track_max_m")), cross_track_limit),
        },
        "terrain_clearance_margin_band": {
            "observed_value": terrain_margin,
            "observed_unit": "m_margin",
            "threshold_value": terrain_soft_margin,
            "threshold_ref": "recovery_window_summary:terrain_soft_margin_m",
            "risk_ratio": _inverse_margin_ratio(terrain_margin, terrain_soft_margin),
        },
        "battery_return_margin_band": {
            "observed_value": return_margin,
            "observed_unit": "percent_margin",
            "threshold_value": battery_margin_reference,
            "threshold_ref": "recovery_policy:battery_return_threshold_percent",
            "risk_ratio": _inverse_margin_ratio(return_margin, battery_margin_reference),
        },
        "progress_stall_band": {
            "observed_value": progress_stall_seconds,
            "observed_unit": "s",
            "threshold_value": progress_stall_threshold_s,
            "threshold_ref": "recovery_window_summary:two_trailing_buckets",
            "risk_ratio": _ratio(progress_stall_seconds, progress_stall_threshold_s),
        },
        "telemetry_stale_band": {
            "observed_value": stale_count,
            "observed_unit": "consecutive_samples",
            "threshold_value": telemetry_stale_threshold_count,
            "threshold_ref": "recovery_window_summary:two_consecutive_stale_samples",
            "risk_ratio": _ratio(stale_count, telemetry_stale_threshold_count),
        },
    }


def _persistence_band(seconds: float) -> str:
    if seconds <= 0.0:
        return "none"
    if seconds < 10.0:
        return "under_10s"
    if seconds < 30.0:
        return "10s_to_30s"
    return "above_30s"


def _persistence_band_index(name: Any) -> int:
    order = ("none", "under_10s", "10s_to_30s", "above_30s")
    try:
        return order.index(str(name))
    except ValueError:
        return 0


def _time_to_limit_band_index(name: Any) -> int:
    order = (
        "not_converging",
        "beyond_120s",
        "within_120s",
        "within_30s",
        "within_10s",
        "limit_reached",
    )
    try:
        return order.index(str(name))
    except ValueError:
        return 0


def _decision_persistence(name: str, value: Mapping[str, Any]) -> Any:
    band_index = _band_index(value.get("band"))
    if (
        name in {"progress_stall_band", "telemetry_stale_band"}
        and band_index is not None
        and band_index >= _band_index("limit_to_1_25x")
    ):
        return value.get("breach_persistence_band")
    return None


def _decision_trend(name: str, value: Mapping[str, Any]) -> str:
    # Stall and telemetry-loss duration already have explicit persistence
    # bands. Including their one-window numeric slope as well would turn a
    # harmless short pause/dropout into a new signature before persistence is
    # established.
    if name in {"progress_stall_band", "telemetry_stale_band"}:
        return "not_worsening"
    return (
        "worsening"
        if value.get("trend") == "worsening"
        and value.get("pending_band") in (None, "")
        else "not_worsening"
    )


def _decision_time_to_limit(name: str, value: Mapping[str, Any]) -> str | None:
    if _decision_trend(name, value) != "worsening":
        return None
    current = str(value.get("time_to_limit_band") or "")
    if current in {"within_10s", "within_30s"}:
        return current
    return None


def _time_to_limit_band(
    *,
    current_risk: float,
    prior_risk: float | None,
    elapsed_delta_s: float,
) -> str:
    if current_risk >= 1.0:
        return "limit_reached"
    if prior_risk is None or elapsed_delta_s <= 0.0 or current_risk <= prior_risk:
        return "not_converging"
    rate = (current_risk - prior_risk) / elapsed_delta_s
    if rate <= 0.0:
        return "not_converging"
    seconds = (1.0 - current_risk) / rate
    if seconds <= 10.0:
        return "within_10s"
    if seconds <= 30.0:
        return "within_30s"
    if seconds <= 120.0:
        return "within_120s"
    return "beyond_120s"


def build_semantic_numeric_state(
    summary: Mapping[str, Any],
    *,
    telemetry_snapshot: Mapping[str, Any],
    recovery_policy: Mapping[str, Any],
    prior_state: Mapping[str, Any] | None = None,
    hysteresis_fraction: float = _DEFAULT_HYSTERESIS_FRACTION,
    debounce_observations: int = _DEFAULT_DEBOUNCE_OBSERVATIONS,
) -> dict[str, Any]:
    """Build policy-relative semantic state without making an action choice."""

    prior_state = _mapping(prior_state)
    prior_dimensions = _mapping(prior_state.get("dimensions"))
    current_elapsed = _number(summary.get("window_end_elapsed_s"))
    if current_elapsed is None:
        current_elapsed = _number(telemetry_snapshot.get("elapsed_seconds"))
    prior_elapsed = _number(prior_state.get("observed_at_elapsed_s"))
    elapsed_delta_s = (
        max(0.0, current_elapsed - prior_elapsed)
        if current_elapsed is not None and prior_elapsed is not None
        else 0.0
    )
    minimum_debounce_interval_s = max(
        1.0,
        _number(summary.get("bucket_s")) or 5.0,
    )
    observations = _semantic_observations(
        summary,
        telemetry_snapshot=telemetry_snapshot,
        recovery_policy=recovery_policy,
    )
    dimensions: dict[str, dict[str, Any]] = {}
    for name, observation in observations.items():
        prior = _mapping(prior_dimensions.get(name))
        risk_ratio = _number(observation.get("risk_ratio"))
        if risk_ratio is None:
            dimensions[name] = {
                **observation,
                "evidence_status": "unavailable",
                "band": prior.get("band") or "unavailable",
                "raw_band": "unavailable",
                "pending_band": None,
                "pending_observations": 0,
                "trend": "unknown",
                "time_to_limit_band": "unknown",
                "breach_persistence_s": _number(prior.get("breach_persistence_s")) or 0.0,
                "breach_persistence_band": prior.get("breach_persistence_band") or "none",
            }
            continue

        raw_index = _raw_band_index(risk_ratio)
        candidate_index = _hysteresis_band_index(
            risk_ratio,
            prior_band=prior.get("band"),
            hysteresis_fraction=max(0.0, float(hysteresis_fraction)),
        )
        prior_index = _band_index(prior.get("band"))
        effective_index = candidate_index
        pending_band: str | None = None
        pending_count = 0
        if prior_index is not None and candidate_index != prior_index:
            prior_pending_band = str(prior.get("pending_band") or "")
            candidate_band = _BANDS[candidate_index]
            can_advance = elapsed_delta_s >= minimum_debounce_interval_s
            pending_band = candidate_band
            pending_count = (
                int(_number(prior.get("pending_observations")) or 0) + 1
                if prior_pending_band == candidate_band and can_advance
                else 1
            )
            if pending_count >= max(1, int(debounce_observations)):
                effective_index = candidate_index
                pending_band = None
                pending_count = 0
            else:
                effective_index = prior_index

        prior_risk = _number(prior.get("risk_ratio"))
        risk_delta = risk_ratio - prior_risk if prior_risk is not None else 0.0
        trend = "stable"
        if prior_risk is None:
            trend = "baseline"
        elif risk_delta >= _MATERIAL_TREND_DELTA_RATIO:
            trend = "worsening"
        elif risk_delta <= -_MATERIAL_TREND_DELTA_RATIO:
            trend = "improving"

        persistence_s = 0.0
        if risk_ratio >= 1.0:
            prior_persistence = _number(prior.get("breach_persistence_s")) or 0.0
            if prior_risk is not None and prior_risk >= 1.0:
                persistence_s = prior_persistence + elapsed_delta_s
        if name == "progress_stall_band":
            persistence_s = max(
                persistence_s,
                _number(observation.get("observed_value")) or 0.0,
            )

        dimensions[name] = {
            **observation,
            "evidence_status": "observed",
            "risk_ratio": round(risk_ratio, 6),
            "band": _BANDS[effective_index],
            "raw_band": _BANDS[raw_index],
            "pending_band": pending_band,
            "pending_observations": pending_count,
            "trend": trend,
            "risk_ratio_delta": round(risk_delta, 6),
            "time_to_limit_band": _time_to_limit_band(
                current_risk=risk_ratio,
                prior_risk=prior_risk,
                elapsed_delta_s=elapsed_delta_s,
            ),
            "breach_persistence_s": round(persistence_s, 3),
            "breach_persistence_band": _persistence_band(persistence_s),
        }

    return {
        "schema_version": SEMANTIC_NUMERIC_STATE_SCHEMA_VERSION,
        "decision_signature_version": RECOVERY_DECISION_SIGNATURE_VERSION,
        "source": "missionos_recovery_window_summary",
        "observed_at_elapsed_s": current_elapsed,
        "hysteresis_fraction": float(hysteresis_fraction),
        "debounce_observations": int(debounce_observations),
        "dimensions": dimensions,
        "proposal_created": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
        "completion_claimed": False,
    }


def semantic_numeric_state_machine_hash(state: Mapping[str, Any]) -> str:
    dimensions = _mapping(state.get("dimensions"))
    material = {
        name: {
            "band": _mapping(value).get("band"),
            "pending_band": _mapping(value).get("pending_band"),
            "pending_observations": _mapping(value).get("pending_observations"),
            "breach_persistence_band": _decision_persistence(
                name,
                _mapping(value),
            ),
        }
        for name, value in sorted(dimensions.items())
    }
    return _canonical_sha256(material)


def build_semantic_numeric_delta(
    prior_state: Mapping[str, Any] | None,
    current_state: Mapping[str, Any],
) -> dict[str, Any]:
    prior_state = _mapping(prior_state)
    prior_dimensions = _mapping(prior_state.get("dimensions"))
    current_dimensions = _mapping(current_state.get("dimensions"))
    observed_changed_dimensions: list[str] = []
    material_changed_dimensions: list[str] = []
    changes: dict[str, Any] = {}
    if prior_dimensions:
        for name, current_raw in sorted(current_dimensions.items()):
            current = _mapping(current_raw)
            prior = _mapping(prior_dimensions.get(name))
            reasons: list[str] = []
            prior_band_index = _band_index(prior.get("band"))
            current_band_index = _band_index(current.get("band"))
            band_changed = prior_band_index != current_band_index
            if band_changed:
                reasons.append("band_changed")
            prior_persistence_index = _persistence_band_index(
                prior.get("breach_persistence_band")
            )
            current_persistence_index = _persistence_band_index(
                current.get("breach_persistence_band")
            )
            persistence_worsened = bool(
                name in {"progress_stall_band", "telemetry_stale_band"}
                and current_band_index is not None
                and current_band_index >= _band_index("limit_to_1_25x")
                and current.get("pending_band") in (None, "")
                and current_persistence_index > prior_persistence_index
            )
            if persistence_worsened:
                reasons.append("persistence_band_changed")
            trend_worsened = bool(
                current.get("trend") == "worsening"
                and prior.get("trend") != "worsening"
                and current.get("pending_band") in (None, "")
                and prior_band_index == current_band_index
                and name not in {"progress_stall_band", "telemetry_stale_band"}
                and (
                    current_band_index is not None
                    and current_band_index >= _band_index("near_limit")
                    or current.get("time_to_limit_band")
                    in {"within_10s", "within_30s"}
                )
            )
            if trend_worsened:
                reasons.append("trend_worsened")
            prior_time_to_limit_index = _time_to_limit_band_index(
                prior.get("time_to_limit_band")
            )
            current_time_to_limit_index = _time_to_limit_band_index(
                current.get("time_to_limit_band")
            )
            time_to_limit_worsened = bool(
                trend_worsened
                and current.get("time_to_limit_band")
                in {"within_10s", "within_30s"}
                and current_time_to_limit_index > prior_time_to_limit_index
            )
            if time_to_limit_worsened:
                reasons.append("time_to_limit_worsened")
            if not reasons:
                continue
            observed_changed_dimensions.append(name)
            material_for_decision_epoch = bool(
                prior_band_index is not None
                and current_band_index is not None
                and current_band_index > prior_band_index
            ) or persistence_worsened or trend_worsened or time_to_limit_worsened
            if material_for_decision_epoch:
                material_changed_dimensions.append(name)
            changes[name] = {
                "reasons": reasons,
                "direction": (
                    "worsening"
                    if material_for_decision_epoch
                    else "improving"
                    if band_changed
                    else "stable"
                ),
                "material_for_decision_epoch": material_for_decision_epoch,
                "prior": {
                    "band": prior.get("band"),
                    "breach_persistence_band": prior.get("breach_persistence_band"),
                },
                "current": {
                    "band": current.get("band"),
                    "breach_persistence_band": current.get("breach_persistence_band"),
                },
                "observed_values": {
                    "prior": prior.get("observed_value"),
                    "current": current.get("observed_value"),
                    "unit": current.get("observed_unit"),
                },
                "threshold_ref": current.get("threshold_ref"),
                "threshold_value": current.get("threshold_value"),
                "trend": current.get("trend"),
                "time_to_limit_band": current.get("time_to_limit_band"),
            }
    return {
        "schema_version": SEMANTIC_NUMERIC_DELTA_SCHEMA_VERSION,
        "decision_signature_version": RECOVERY_DECISION_SIGNATURE_VERSION,
        "material_change": bool(material_changed_dimensions),
        "changed_dimensions": material_changed_dimensions,
        "observed_changed_dimensions": observed_changed_dimensions,
        "changes": changes,
        "proposal_created": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "progress_counted": False,
        "completion_claimed": False,
    }


def build_semantic_recovery_decision_signature(
    *,
    legacy_signature: str,
    semantic_state: Mapping[str, Any],
    categorical_state: Mapping[str, Any] | None = None,
) -> str:
    """Hash the active v2 decision state without inheriting v1 instability.

    ``legacy_signature`` is accepted so callers can keep producing a shadow
    comparison during migration.  It is deliberately not part of the active
    v2 hash: folding the v1 hash into v2 would make transient boolean threshold
    jitter a new hosted-model decision epoch again.
    """

    dimensions = _mapping(semantic_state.get("dimensions"))
    material = {
        "decision_signature_version": RECOVERY_DECISION_SIGNATURE_VERSION,
        "categorical_state": dict(_mapping(categorical_state)),
        "semantic_numeric_state": {
            name: {
                "band": _mapping(value).get("band"),
                "breach_persistence_band": _decision_persistence(
                    name,
                    _mapping(value),
                ),
                "trend": _decision_trend(name, _mapping(value)),
                "time_to_limit_band": _decision_time_to_limit(
                    name,
                    _mapping(value),
                ),
            }
            for name, value in sorted(dimensions.items())
        },
    }
    # Keep the migration input explicit without letting it affect the active
    # signature. This also prevents an accidental removal before shadow-mode
    # evidence has been retired deliberately.
    _ = legacy_signature
    return _canonical_sha256(material)
