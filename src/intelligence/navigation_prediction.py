"""Read-only, source-bound navigation forecasts shared by mission agents.

Predicted exposure is not a safety probability or dispatch authority. Time uses
one simulator clock domain, including time spent in model/agent calls.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from src.intelligence.mission_assurance_policy import digest


class NavigationPrediction:
    def __init__(self, *, request: dict, prediction: dict, clock_domain: str):
        stamps = request["timestamps"]
        if len(stamps) != 4 or not all(
            type(t) in (int, float) and math.isfinite(t) for t in stamps
        ):
            raise ValueError("four_finite_observation_timestamps_required")
        if any(abs(b - a - 0.5) > 0.15 for a, b in zip(stamps, stamps[1:])):
            raise ValueError("two_hz_observation_context_required")
        hashes = request.get("context_sha256")
        if (
            not isinstance(hashes, list)
            or len(hashes) != 4
            or any(
                not isinstance(h, str)
                or len(h) != 64
                or any(c not in "0123456789abcdef" for c in h)
                for h in hashes
            )
        ):
            raise ValueError("four_source_image_hashes_required")
        provenance = prediction.get("provenance") or {}
        if prediction.get("learned_wam_invoked") is not True or not provenance.get(
            "task_manifest_sha256"
        ):
            raise ValueError("learned_prediction_source_required")
        for key in ("current_exposure", "predicted_exposure"):
            value = prediction.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("finite_exposure_required")
        value: dict[str, Any] = {
            "schema_version": "missionos_navigation_prediction.v1",
            "clock_domain": clock_domain,
            "observed_at_sim_s": stamps[-1],
            "target_at_sim_s": stamps[-1] + 4.0,
            "horizon_s": 4.0,
            "observed_clearance_exposure_threshold": 0.06,
            "forecast_clearance_support_exposure_threshold": 0.2,
            "threshold_semantics": "exposure thresholds for this synthetic benchmark, not collision probabilities",
            "assumption": "robot remains stationary until target time",
            "current_exposure": prediction["current_exposure"],
            "predicted_exposure": prediction["predicted_exposure"],
            "image_motion_consistency": prediction.get("image_motion_consistency"),
            "approaching_obstacle": prediction.get("approaching_obstacle"),
            "context_timestamps": stamps,
            "context_sha256": hashes,
            "provenance": provenance,
            "safety_probability": None,
            "uncertainty": "synthetic red obstacle; camera-only learned forecast; real post-wait observation required",
        }
        value["prediction_ref"] = digest(value)
        self._value = copy.deepcopy(value)

    def read(self) -> dict:
        """Return factual forecast evidence without a selected action or authority."""
        return copy.deepcopy(self._value)

    def current(self, now_sim_s: float, clock_domain: str) -> bool:
        return (
            type(now_sim_s) in (int, float)
            and math.isfinite(now_sim_s)
            and clock_domain == self._value["clock_domain"]
            and self._value["observed_at_sim_s"] <= now_sim_s < self._value["target_at_sim_s"]
        )
