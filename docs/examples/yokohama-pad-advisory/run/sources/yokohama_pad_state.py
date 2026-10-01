"""CPU learned lead localization and short state forecast; no ANWM or aircraft API.

Only a fixed camera, one visible known lead and its recorded corridor are covered.
Image/history quality failure means unknown, never absence or permission to move.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import numpy as np

SCHEMA = "missionos.pad-state-model.v1"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def channels(rgb):
    gray = rgb.astype(np.float64).mean(axis=2) / 255
    padded = np.pad(gray, 2, mode="symmetric")
    smooth = sum(padded[y : y + 180, x : x + 320] for y in range(5) for x in range(5)) / 25
    return np.stack((gray - 0.5, (gray - smooth) * 4))


def correlation(images, kernels):
    """Two-channel valid correlation with NumPy FFT, preserving full image FOV."""
    size = (images.shape[1] + 16, images.shape[2] + 16)
    transformed = np.fft.rfft2(images, s=size, axes=(-2, -1))
    weights = np.fft.rfft2(kernels[:, ::-1, ::-1], s=size, axes=(-2, -1))
    full = np.fft.irfft2((transformed * weights).sum(axis=0), s=size)
    return full[16 : images.shape[1], 16 : images.shape[2]]


def spatial(uv):
    x, y = (uv[:, 0] - 160) / 120, (uv[:, 1] - 90) / 90
    return np.c_[np.ones(len(x)), x, y, x * x, x * y, y * y, x**3, x * x * y, x * y * y, y**3]


def features(positions):
    return np.r_[
        positions[-1] / 10,
        positions[-1] - positions[-5],
        (positions[-1] - positions[-9]) / 2,
        (positions[-1] - positions[-13]) / 3,
    ]


def distance(a, b):
    return np.maximum(0, np.square(a[:, None] - b[None]).sum(axis=2))


def states(prediction, radii):
    radius = np.linalg.norm(prediction[..., :2], axis=-1)
    return np.where(
        (radius - radii > 6.8) & (prediction[..., 3] < 0.25),
        "clear",
        np.where((radius + radii < 5.2) & (prediction[..., 3] > 0.75), "occupied", "unknown"),
    )


class Model:
    def __init__(self, values):
        self.values = values

    @classmethod
    def load(cls, root):
        meta = json.loads((root / "model.json").read_text())
        if meta["schema"] != SCHEMA or sha(root / "model.npz") != meta["weights_sha256"]:
            raise ValueError("State model identity changed")
        with np.load(root / "model.npz", allow_pickle=False) as z:
            values = {k: z[k] for k in z.files}
        required = {
            "weights",
            "low",
            "high",
            "mapping",
            "basis",
            "coefficients",
            "sigma",
            "radii",
            "support_limit",
            "score_floor",
            "margin_floor",
            "camera_pose",
        }
        if set(values) != required or any(not np.isfinite(v).all() for v in values.values()):
            raise ValueError("Invalid model arrays")
        if (
            values["weights"].shape != (579,)
            or values["mapping"].shape != (10, 3)
            or values["basis"].ndim != 2
            or values["basis"].shape[1] != 12
            or values["coefficients"].shape != (len(values["basis"]), 68)
            or values["radii"].shape != (17,)
            or (values["radii"] < 0.2).any()
            or values["sigma"] <= 0
            or values["camera_pose"].shape != (4, 4)
        ):
            raise ValueError("Invalid model geometry")
        return cls(values)

    def scoremap(self, rgb):
        if rgb.shape != (180, 320, 3) or rgb.dtype != np.uint8:
            raise ValueError("Expected measured full-FOV 320x180 RGB")
        v = self.values
        score = correlation(channels(rgb), v["weights"][:-1].reshape(2, 17, 17)) + v["weights"][-1]
        lo, hi = v["low"].astype(int), v["high"].astype(int)
        return score[lo[1] - 8 : hi[1] - 7, lo[0] - 8 : hi[0] - 7]

    def detect(self, rgb):
        score = self.scoremap(rgb)
        y, x = np.unravel_index(score.argmax(), score.shape)
        yy, xx = np.indices(score.shape)
        other = score[np.square(xx - x) + np.square(yy - y) >= 12**2]
        value = float(score[y, x])
        margin = value - float(other.max())
        uv = np.array([x, y]) + self.values["low"]
        point = spatial(uv[None]) @ self.values["mapping"]
        supported = value >= float(self.values["score_floor"]) and margin >= float(
            self.values["margin_floor"]
        )
        return dict(
            uv=uv.tolist(),
            xyz_m=point[0].tolist(),
            score=value,
            margin=margin,
            supported=bool(supported),
        )

    def predict(self, rgb, stamps_ns, camera_poses):
        started = time.perf_counter()
        if rgb.shape != (16, 180, 320, 3) or rgb.dtype != np.uint8:
            raise ValueError("Exactly sixteen measured images required")
        if (
            stamps_ns.shape != (16,)
            or stamps_ns.dtype.kind not in "iu"
            or not np.all(np.abs(np.diff(stamps_ns) - 250_000_000) <= 4_000_001)
        ):
            raise ValueError("Noncontiguous 4 Hz observation history")
        if (
            camera_poses.shape != (16, 4, 4)
            or not np.isfinite(camera_poses).all()
            or not np.allclose(camera_poses, self.values["camera_pose"], atol=1e-7, rtol=0)
        ):
            raise ValueError("Unqualified moving or changed camera")
        detections = [self.detect(frame) for frame in rgb]
        points = np.array([d["xyz_m"] for d in detections])
        f = features(points)[None]
        distances = distance(f, self.values["basis"])
        support_distance = float(np.sqrt(distances.min()))
        supported = all(d["supported"] for d in detections) and support_distance <= float(
            self.values["support_limit"]
        )
        kernel = np.exp(-distances / (2 * float(self.values["sigma"]) ** 2))
        predicted = (kernel @ self.values["coefficients"]).reshape(17, 4)
        predicted[:, :3] += points[-1]
        labels = states(predicted, self.values["radii"]) if supported else np.full(17, "unknown")
        forecasts = [
            dict(
                offset_s=i / 4,
                stamp_ns=int(stamps_ns[-1]) + i * 250_000_000,
                xyz_relative_to_pad_m=p[:3].tolist() if supported else None,
                occupancy_score=float(np.clip(p[3], 0, 1)) if supported else None,
                empirical_xy_radius_m=float(self.values["radii"][i]),
                state=str(labels[i]),
            )
            for i, p in enumerate(predicted)
        ]
        return dict(
            schema="missionos.pad-state-forecast.v1",
            supported=bool(supported),
            input_last_stamp_ns=int(stamps_ns[-1]),
            detections=detections,
            support_distance=support_distance,
            forecasts=forecasts,
            compute_seconds=time.perf_counter() - started,
            model_kind="learned CPU object/state model; not ANWM or VLA",
            interval_collision_verified=False,
            action_conditioning_verified=False,
            flight_admitted=False,
            dispatch_invoked=False,
        )


def fit_detector(sequences):
    uv = np.concatenate([s["uv"] for s in sequences])
    low, high = np.floor(uv.min(axis=0) - 12).astype(int), np.ceil(uv.max(axis=0) + 12).astype(int)
    if (low < [8, 8]).any() or (high > [311, 171]).any():
        raise ValueError("Supervised lead leaves detector FOV")
    random = np.random.default_rng(42)
    x, y = [], []
    for sequence in sequences:
        for i in range(0, len(sequence["rgb"]), 4):
            ch = channels(sequence["rgb"][i])
            u, v = np.round(sequence["uv"][i]).astype(int)
            for du, dv, target in [(0, 0, 1), (-1, 0, 0.8), (1, 0, 0.8), (0, -1, 0.8), (0, 1, 0.8)]:
                x.append(
                    np.r_[ch[:, v + dv - 8 : v + dv + 9, u + du - 8 : u + du + 9].flatten(), 1]
                )
                y.append(target)
            for _ in range(16):
                while True:
                    a, b = random.integers(low, high + 1)
                    if (a - u) ** 2 + (b - v) ** 2 > 12**2:
                        break
                x.append(np.r_[ch[:, b - 8 : b + 9, a - 8 : a + 9].flatten(), 1])
                y.append(0)
    x, y = np.array(x), np.array(y)

    def fit():
        return np.linalg.solve(x.T @ x + np.eye(x.shape[1]) * 0.5, x.T @ y)

    values = dict(
        weights=fit(),
        low=low,
        high=high,
        mapping=np.zeros((10, 3)),
        score_floor=np.array(0.7),
        margin_floor=np.array(0.1),
    )
    model, trace = Model(values), []
    # Hard negative examples come only from training frames, including shadows.
    for iteration in range(3):
        hard = []
        yy, xx = np.mgrid[low[1] : high[1] + 1, low[0] : high[0] + 1]
        for sequence in sequences:
            for i in range(0, len(sequence["rgb"]), 4):
                rgb = sequence["rgb"][i]
                ch = channels(rgb)
                scores = model.scoremap(rgb).copy()
                actual = sequence["uv"][i]
                scores[(xx - actual[0]) ** 2 + (yy - actual[1]) ** 2 < 9**2] = -100
                for _ in range(3):
                    v, u = np.unravel_index(scores.argmax(), scores.shape)
                    v += low[1]
                    u += low[0]
                    hard.append(np.r_[ch[:, v - 8 : v + 9, u - 8 : u + 9].flatten(), 1])
                    scores[(xx - u) ** 2 + (yy - v) ** 2 < 9**2] = -100
        x = np.r_[x, np.array(hard)]
        y = np.r_[y, np.zeros(len(hard))]
        values["weights"] = fit()
        trace.append(
            dict(
                iteration=iteration + 1,
                patches=len(x),
                mse=float(np.square(x @ values["weights"] - y).mean()),
            )
        )
    detected = np.concatenate([[model.detect(im)["uv"] for im in s["rgb"]] for s in sequences])
    geometry = spatial(detected)
    values["mapping"] = np.linalg.solve(
        geometry.T @ geometry + np.eye(10) * 1e-5,
        geometry.T @ np.concatenate([s["xyz"] for s in sequences]),
    )
    return model, trace


def temporal_samples(model, sequences):
    x, y, base, truth, detections = [], [], [], [], []
    for sequence in sequences:
        detected = [model.detect(im) for im in sequence["rgb"]]
        detections.extend(detected)
        positions = np.array([d["xyz_m"] for d in detected])
        actual = sequence["xyz"]
        radius = np.linalg.norm(actual[:, :2], axis=1)
        occupied = np.where(radius < 5.2, 1, np.where(radius > 6.8, 0, 0.5))
        for i in range(15, len(positions) - 16, 4):
            x.append(features(positions[i - 15 : i + 1]))
            y.append(np.c_[actual[i : i + 17] - positions[i], occupied[i : i + 17]].flatten())
            base.append(positions[i])
            truth.append(np.c_[actual[i : i + 17], occupied[i : i + 17]])
    return np.array(x), np.array(y), np.array(base), np.array(truth), detections


def fit(train, development):
    started = time.perf_counter()
    model, detector_trace = fit_detector(train)
    x, y, _, _, train_detected = temporal_samples(model, train)
    dev, _, base, truth, dev_detected = temporal_samples(model, development)
    squared = distance(x, x)
    base_sigma = float(np.sqrt(np.median(squared[squared > 1e-8])))
    trials, models = [], []
    for factor in (0.25, 0.5, 1, 2):
        for regularization in (0.001, 0.01, 0.1):
            sigma = base_sigma * factor
            coefficients = np.linalg.solve(
                np.exp(-squared / (2 * sigma * sigma)) + np.eye(len(x)) * regularization, y
            )
            prediction = (np.exp(-distance(dev, x) / (2 * sigma * sigma)) @ coefficients).reshape(
                -1, 17, 4
            )
            prediction[:, :, :3] += base[:, None]
            xy_error = np.linalg.norm(prediction[:, :, :2] - truth[:, :, :2], axis=2)
            radii = np.maximum(0.2, np.quantile(xy_error, 0.99, axis=0) + 0.1)
            labels = states(prediction, radii)
            expected = np.where(
                truth[:, :, 3] == 1, "occupied", np.where(truth[:, :, 3] == 0, "clear", "unknown")
            )
            known = expected != "unknown"
            row = dict(
                factor=factor,
                regularization=regularization,
                mean_xyz_error_m=float(
                    np.linalg.norm(prediction[:, :, :3] - truth[:, :, :3], axis=2).mean()
                ),
                known_match_fraction=float((labels[known] == expected[known]).mean()),
                false_clear=int(((labels == "clear") & (expected == "occupied")).sum()),
                unknown_fraction=float((labels[known] == "unknown").mean()),
            )
            trials.append(row)
            models.append((sigma, coefficients, radii))
    selected = min(
        range(len(trials)),
        key=lambda i: (
            trials[i]["false_clear"],
            -trials[i]["known_match_fraction"],
            trials[i]["mean_xyz_error_m"],
        ),
    )
    sigma, coefficients, radii = models[selected]
    model.values.update(
        basis=x,
        coefficients=coefficients,
        sigma=np.array(sigma),
        radii=radii,
        support_limit=np.array(max(0.1, float(np.sqrt(distance(dev, x).min(axis=1)).max()) * 1.25)),
        camera_pose=train[0]["camera_poses"][0],
    )
    stats = []
    for split, sequences, detected in [
        ("train", train, train_detected),
        ("development", development, dev_detected),
    ]:
        measured = np.concatenate([s["xyz"] for s in sequences])
        error = np.linalg.norm(np.array([d["xyz_m"] for d in detected]) - measured, axis=1)
        stats.append(
            dict(
                split=split,
                frames=len(detected),
                mean_xyz_error_m=float(error.mean()),
                q99_xyz_error_m=float(np.quantile(error, 0.99)),
                unsupported_frames=sum(not d["supported"] for d in detected),
            )
        )
    return model, dict(
        detector_trace=detector_trace,
        trials=trials,
        selected_trial=selected,
        localization=stats,
        training_seconds=time.perf_counter() - started,
        training_windows=len(x),
        development_windows=len(dev),
        seed=42,
        model_kind="supervised convolution/ridge localization + kernel-ridge state forecast",
        anwm_weights_changed=False,
        vla_training=False,
        gpu_requested=False,
        flight_admitted=False,
    )
