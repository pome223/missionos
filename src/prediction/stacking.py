"""Optional local ExtraTrees adapter for the staged stacking mission.

Only explicit, digest-pinned trusted local pickle checkpoints may be loaded.
No checkpoint, training corpus, or simulator is distributed by this module.
"""

from __future__ import annotations

import hashlib
import pickle
import warnings
from pathlib import Path

import numpy as np
from missionos_core.prediction import (
    OptionForecast,
    PredictionBinding,
    PredictionRequest,
)

INPUT_SCHEMA = "stacking.exact_state.v1"
MISSION = "stacking.max10.uniform_terminal_hold14.2.v1"
ENVIRONMENT = "mujoco.panda.staged_cuboids20hz.v1"
MACRO = [0.08, 0.008, 60, 20, 60, 12, 60, 72, 20]


def rotation(q):
    w, x, y, z = q
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def reference_pose(x, option=0):
    pose = x["objects"].copy()
    if option == 0:
        pose[int(x["count"]) - 1] = np.r_[x["plan"][:3], 1, 0, 0, 0]
    return pose


def features(x, option=0):
    """Preserve the frozen model's feature order and numeric operations."""
    original_n = int(x["count"])
    n = original_n - option
    pose = reference_pose(x, option)
    origin = np.r_[x["plan"][:2], 0.8]
    physics = x["physics"].copy()
    mask = np.arange(10) < n
    existing = np.arange(10) < original_n - 1
    velocity = x["velocity"].copy()
    velocity[~existing] = 0
    displacement = x["objects"][:, :3] - x["accepted"]
    displacement[~existing] = 0
    com = np.array([pose[i, :3] + rotation(pose[i, 3:]) @ physics[i, 7:10] for i in range(10)])
    margins = np.zeros((10, 6))
    for i in range(n):
        mass = physics[i:n, 3].sum()
        center = (com[i:n] * physics[i:n, 3, None]).sum(0) / mass
        support = pose[max(i - 1, 0), :3]
        dims = physics[i, :3] if i == 0 else np.minimum(physics[i, :3], physics[i - 1, :3])
        diff = center - support
        margins[i] = [
            dims[0] / 2 - abs(diff[0]),
            dims[1] / 2 - abs(diff[1]),
            mass,
            diff[0],
            diff[1],
            np.linalg.norm(velocity[i, :3]),
        ]
    pose[:, :3] -= origin
    pose[~mask] = 0
    physics[~mask] = 0
    robot = x["robot"].copy()
    robot[-3:] -= robot[:3]
    robot[:3] -= origin
    plan = x["plan"].copy()
    plan[:3] -= origin
    if option == 1:
        robot[-3:] = 0
        plan[:] = 0
    return np.r_[
        pose.ravel(),
        velocity.ravel(),
        physics.ravel(),
        displacement.ravel(),
        mask,
        robot,
        plan,
        margins.ravel(),
        option,
    ].astype("float32")


def validate_state(state):
    shapes = {
        "objects": (10, 7),
        "velocity": (10, 6),
        "physics": (10, 11),
        "robot": (12,),
        "count": (),
        "accepted": (10, 3),
        "plan": (12,),
    }
    if set(state) != set(shapes):
        raise ValueError("state schema mismatch")
    x = {k: np.asarray(v, dtype=float) for k, v in state.items()}
    if any(x[k].shape != shape or not np.isfinite(x[k]).all() for k, shape in shapes.items()):
        raise ValueError("invalid state shape or value")
    if not 1 <= x["count"] <= 10 or int(x["count"]) != x["count"]:
        raise ValueError("invalid count")
    if not np.array_equal(x["plan"][3:], MACRO):
        raise ValueError("unsupported control macro")
    phys = x["physics"]
    if not np.all(phys[:, 10] == 1):
        raise ValueError("unsupported shape")
    if (phys[:, :4] <= 0).any() or (phys[:, 4:7] < 0).any():
        raise ValueError("invalid physics")
    if not np.allclose(np.linalg.norm(x["objects"][:, 3:], axis=1), 1, atol=1e-5):
        raise ValueError("invalid object orientation")
    return x


class StackingPredictor:
    def __init__(self, path: Path, digest: str, policy_sha256: str, *, model_id="stacking-wam"):
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("checkpoint digest mismatch")
        from sklearn.exceptions import InconsistentVersionWarning

        with warnings.catch_warnings():
            warnings.simplefilter("error", InconsistentVersionWarning)
            self.model = pickle.loads(data)  # Explicit trusted-local checkpoint only.
        self.threshold = float(self.model["threshold"])
        self.horizon_steps = int(self.model.get("horizon_steps", 284))
        if self.horizon_steps not in (284, 568) or not 0 <= self.threshold <= 1:
            raise ValueError("unsupported model contract")
        self.binding = PredictionBinding(
            model_id, digest, MISSION, policy_sha256, ENVIRONMENT, INPUT_SCHEMA
        )

    def predict(self, request: PredictionRequest) -> tuple[OptionForecast, ...]:
        x = validate_state(request.state)
        options = request.options
        if [(o.option_id, o.horizon_seconds) for o in options] != [
            ("continue", self.horizon_steps / 20),
            ("bank", 14.2),
        ]:
            raise ValueError("unsupported options/horizons")
        if any(o.parameters != {"macro": "stacking.fixed_vla_placement.v1"} for o in options):
            raise ValueError("unsupported action parameters")
        z = np.stack([features(x, i) for i in (0, 1)])
        risk = self.model["classifier"].predict_proba(z)[:, 1]
        regression = self.model["regressor"].predict(z)
        forecasts = []
        for i, option in enumerate(options):
            pose = reference_pose(x, i) + regression[i, 10:].reshape(10, 7)
            norm = np.linalg.norm(pose[:, 3:], axis=1, keepdims=True)
            pose[:, 3:] /= np.maximum(norm, 1e-9)
            forecasts.append(
                OptionForecast(
                    option.option_id,
                    option.horizon_seconds,
                    float(risk[i]),
                    {
                        "object_poses": pose.tolist(),
                        "per_object_drop_m": regression[i, :10].clip(0).tolist(),
                    },
                )
            )
        return tuple(forecasts)
