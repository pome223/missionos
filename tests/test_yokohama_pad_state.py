"""The learned image state must not turn missing evidence into movement authority."""

import inspect
import json

import numpy as np
import pytest

from scripts.yokohama_pad_state import Model, SCHEMA, correlation, sha


@pytest.fixture
def model():
    return Model(
        dict(
            weights=np.zeros(579),
            low=np.array([151, 27]),
            high=np.array([284, 133]),
            mapping=np.zeros((10, 3)),
            basis=np.zeros((1, 12)),
            coefficients=np.zeros((1, 68)),
            sigma=np.array(1.0),
            radii=np.full(17, 0.2),
            support_limit=np.array(1.0),
            score_floor=np.array(0.7),
            margin_floor=np.array(0.1),
            camera_pose=np.eye(4),
        )
    )


def inputs():
    return (
        np.zeros((16, 180, 320, 3), np.uint8),
        np.arange(16, dtype=np.int64) * 250_000_000,
        np.repeat(np.eye(4)[None], 16, axis=0),
    )


def test_no_detection_is_unknown_and_has_no_flight_authority(model):
    result = model.predict(*inputs())
    assert not result["supported"]
    assert {f["state"] for f in result["forecasts"]} == {"unknown"}
    assert all(f["xyz_relative_to_pad_m"] is None for f in result["forecasts"])
    assert not result["flight_admitted"] and not result["dispatch_invoked"]
    assert not result["interval_collision_verified"] and not result["action_conditioning_verified"]


@pytest.mark.parametrize(
    "fault", ["duplicate", "gap", "reverse", "float", "shape", "camera", "nan"]
)
def test_broken_history_is_rejected(model, fault):
    rgb, stamps, poses = inputs()
    if fault == "duplicate":
        stamps[8] = stamps[7]
    elif fault == "gap":
        stamps[8:] += 250_000_000
    elif fault == "reverse":
        stamps = stamps[::-1]
    elif fault == "float":
        stamps = stamps.astype(float)
    elif fault == "shape":
        rgb = rgb[:-1]
    elif fault == "camera":
        poses[-1, 0, 3] = 0.001
    else:
        poses[-1, 0, 3] = np.nan
    with pytest.raises(ValueError):
        model.predict(rgb, stamps, poses)


def test_future_state_and_schedule_are_not_inference_inputs():
    assert list(inspect.signature(Model.predict).parameters) == [
        "self",
        "rgb",
        "stamps_ns",
        "camera_poses",
    ]


def test_outside_training_support_is_unknown(model, monkeypatch):
    monkeypatch.setattr(
        model,
        "detect",
        lambda rgb: dict(
            uv=[160, 90], xyz_m=[30.0, 0.0, 0.0], score=1.0, margin=1.0, supported=True
        ),
    )
    result = model.predict(*inputs())
    assert not result["supported"]
    assert all(f["state"] == "unknown" for f in result["forecasts"])


def test_identity_is_checked_on_reload(model, tmp_path):
    np.savez_compressed(tmp_path / "model.npz", **model.values)
    (tmp_path / "model.json").write_text(
        json.dumps(dict(schema=SCHEMA, weights_sha256=sha(tmp_path / "model.npz")))
    )
    loaded = Model.load(tmp_path)
    assert not loaded.predict(*inputs())["flight_admitted"]
    with (tmp_path / "model.npz").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="identity"):
        Model.load(tmp_path)


def test_fft_correlation_matches_direct_dot_product():
    random = np.random.default_rng(10)
    images = random.normal(size=(2, 22, 25))
    kernel = random.normal(size=(2, 17, 17))
    actual = correlation(images, kernel)
    expected = np.array(
        [[(images[:, y : y + 17, x : x + 17] * kernel).sum() for x in range(9)] for y in range(6)]
    )
    np.testing.assert_allclose(actual, expected, atol=1e-11)
