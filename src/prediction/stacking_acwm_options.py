"""Opt-in two-option ACWM evidence for placement+hold and stopping.

An explicit backend must generate both movies from the same current observation.
No model service, credentials, judgment, or execution is started on import.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import re
import numpy as np
from missionos_core.prediction import (
    OptionForecast,
    PredictionBinding,
    PredictionRequest,
)
from .stacking import ENVIRONMENT, MISSION
from .stacking_acwm import MACRO_ID, validate_acwm_input
from .stacking_acwm_mission import GovernedACWMStackingSession

HORIZONS = {"continue": 28.4, "bank": 14.2}


class StackingACWMOptionsPredictor:
    threshold = 0.5
    horizon_steps = 568  # continue forecast includes placement and terminal hold

    def __init__(
        self,
        backend: Callable[[dict, str], Mapping],
        *,
        model_sha256: str,
        readout_sha256: str,
        policy_sha256: str,
    ):
        if not callable(backend) or any(
            not re.fullmatch(r"[0-9a-f]{64}", x)
            for x in (model_sha256, readout_sha256, policy_sha256)
        ):
            raise ValueError("explicit backend and pinned digests required")
        self.backend = backend
        self.readout_sha256 = readout_sha256
        self.binding = PredictionBinding(
            "stacking-online-acwm-options",
            model_sha256,
            MISSION,
            policy_sha256,
            ENVIRONMENT,
            "stacking.current_image_macro_options.v1",
        )

    def predict(self, request: PredictionRequest) -> tuple[OptionForecast, ...]:
        if request.binding != self.binding:
            raise ValueError("binding mismatch")
        if len(request.options) != 2 or {o.option_id for o in request.options} != set(
            HORIZONS
        ):
            raise ValueError("both continue and bank required")
        for o in request.options:
            if o.horizon_seconds != HORIZONS[o.option_id] or o.parameters != {
                "macro": MACRO_ID,
                "readout_sha256": self.readout_sha256,
            }:
                raise ValueError("unsupported option or horizon")
        state = validate_acwm_input(request.state)
        forecasts = []
        for option, horizon in HORIZONS.items():
            result = self.backend({k: v.copy() for k, v in state.items()}, option)
            if (
                result["model_sha256"] != self.binding.model_sha256
                or result["readout_sha256"] != self.readout_sha256
                or result["macro_id"] != MACRO_ID
                or result["option_id"] != option
                or result["horizon_seconds"] != horizon
            ):
                raise ValueError("backend artifact or option mismatch")
            risk = float(result["readout_output"])
            if not np.isfinite(risk) or not 0 <= risk <= 1:
                raise ValueError("invalid visual readout")
            forecasts.append(
                OptionForecast(
                    option,
                    horizon,
                    risk,
                    {
                        "representation": "generated_video_visual_readout",
                        "risk_semantics": "uncalibrated_classifier_output",
                        "readout_sha256": self.readout_sha256,
                        "generation_invocation_id": str(result["invocation_id"]),
                    },
                )
            )
        return tuple(forecasts)


class GovernedACWMOptionsSession(GovernedACWMStackingSession):
    model_limit = (
        "Neural future-video generation from exact current simulator information. "
        "Continue covers placement plus hold (28.4 seconds); bank covers stopping "
        "and holding (14.2 seconds). Both visual classifier outputs are uncalibrated."
    )

    def score_evidence(self, forecast, next_count):
        options = {f["option_id"]: f for f in forecast["forecasts"]}
        return {
            "schema_version": "stacking_acwm_options_scope.v1",
            "status": "both_option_forecasts_available",
            "current_count": next_count - 1,
            "next_count": next_count,
            "risk_meaning": "Uncalibrated generated-video classifier scores, not physical probabilities.",
            "positive_class": "A score-bearing block drops more than 0.03 m during the option horizon.",
            "continue_horizon_seconds": 28.4,
            "bank_horizon_seconds": 14.2,
            "continue_risk": options["continue"]["risk_score"],
            "bank_risk": options["bank"]["risk_score"],
            "bank_forecast_available": True,
            "terminal_hold_forecast_available": True,
            "bank_proxy_points": None,
            "continue_then_bank_proxy_points": None,
            "calibrated": False,
            "recommended_option": None,
            "dispatch_authority_created": False,
        }
