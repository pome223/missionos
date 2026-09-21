"""Opt-in, tape-free ACWM boundary; a forecast carries no execution authority.

The caller supplies an explicit inference backend. No GPU, model download,
network connection, stopping policy, or executor is enabled by importing this.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping

import numpy as np
from missionos_core.prediction import OptionForecast, PredictionBinding, PredictionRequest

from .stacking import ENVIRONMENT, MACRO, MISSION

INPUT_SCHEMA = "stacking.current_image_macro.v1"
MACRO_ID = "smolvla-physical-tower-placement-284-v1"


def validate_acwm_input(state: dict) -> dict[str, np.ndarray]:
    shapes = {"image": (240, 240, 3), "objects": (10, 7), "velocity": (10, 6),
              "physics": (10, 11), "robot": (12,), "count": (), "plan": (12,)}
    if set(state) != set(shapes):
        raise ValueError("only current image/state and registered plan are accepted")
    x = {k: np.asarray(v) for k, v in state.items()}
    if any(x[k].shape != shape or not np.isfinite(x[k]).all() for k, shape in shapes.items()):
        raise ValueError("invalid current observation")
    if not 1 <= x["count"] <= 10 or int(x["count"]) != x["count"]:
        raise ValueError("invalid count")
    image = x["image"]
    if (image < 0).any() or (image > 255).any() or not np.equal(image, np.floor(image)).all():
        raise ValueError("image must contain byte values")
    if not np.array_equal(x["plan"][3:], MACRO):
        raise ValueError("unregistered macro parameters")
    if (x["physics"][:, :4] <= 0).any() or (x["physics"][:, 4:7] < 0).any():
        raise ValueError("invalid physical properties")
    if not np.all(x["physics"][:, 10] == 1):
        raise ValueError("unsupported object shape")
    if not np.allclose(np.linalg.norm(x["objects"][:, 3:], axis=1), 1, atol=1e-5):
        raise ValueError("invalid orientations")
    x["image"] = image.astype(np.uint8)
    return x


class StackingACWMPredictor:
    def __init__(self, backend: Callable[[dict], Mapping], *, model_sha256: str,
                 readout_sha256: str, policy_sha256: str):
        if not callable(backend) or any(not re.fullmatch(r"[0-9a-f]{64}", d)
                                       for d in (model_sha256, readout_sha256, policy_sha256)):
            raise ValueError("explicit backend and pinned digests required")
        self.backend = backend
        self.readout_sha256 = readout_sha256
        self.binding = PredictionBinding("stacking-online-acwm", model_sha256,
                                         MISSION, policy_sha256, ENVIRONMENT, INPUT_SCHEMA)

    def predict(self, request: PredictionRequest) -> tuple[OptionForecast, ...]:
        if request.binding != self.binding:
            raise ValueError("binding mismatch")
        if len(request.options) != 1:
            raise ValueError("this model forecasts the placement option only")
        option = request.options[0]
        if (option.option_id, option.horizon_seconds, option.parameters) != (
            "continue", 14.2, {"macro": MACRO_ID, "readout_sha256": self.readout_sha256}
        ):
            raise ValueError("unsupported option or horizon")
        result = self.backend(validate_acwm_input(request.state))
        if result["model_sha256"] != self.binding.model_sha256 or result["readout_sha256"] != self.readout_sha256:
            raise ValueError("backend artifact mismatch")
        if result["horizon_seconds"] != 14.2 or result["macro_id"] != MACRO_ID:
            raise ValueError("backend horizon or macro mismatch")
        score = float(result["readout_output"])
        if not np.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("invalid readout")
        return (OptionForecast("continue", 14.2, score, {
            "representation": "generated_video_visual_readout",
            "risk_semantics": "uncalibrated_classifier_output",
            "readout_sha256": self.readout_sha256,
            "generation_invocation_id": str(result["invocation_id"]),
        }),)
