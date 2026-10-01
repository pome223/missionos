import numpy as np
import pytest

from scripts.analyze_yokohama_pad_denoising import alpha_bar, check_diagnostics, x0_mse_from_epsilon


@pytest.mark.parametrize("t", [100, 500, 900])
def test_epsilon_conversion_matches_direct_x0_error(t):
    rng = np.random.default_rng(41)
    x0, eps, delta = rng.normal(size=(3, 4, 28, 28))
    a = alpha_bar()[t]
    xt = np.sqrt(a) * x0 + np.sqrt(1 - a) * eps
    reconstructed = (xt - np.sqrt(1 - a) * (eps + delta)) / np.sqrt(a)
    actual = np.mean((reconstructed - x0) ** 2)
    assert x0_mse_from_epsilon(float(np.mean(delta**2)), t) == pytest.approx(actual)


def test_teacher_coverage_and_boundary_are_required():
    rows = [
        dict(
            step=step,
            timestep=t,
            kind="teacher_forced",
            roi_noise_mse=0.1,
            background_noise_mse=0.2,
        )
        for step in (0, 256, 1024)
        for t in (100, 500, 900)
    ]
    check_diagnostics(rows, [0, 256, 1024])
    with pytest.raises(ValueError, match="Missing or duplicate"):
        check_diagnostics(rows[:-1], [0, 256, 1024])
    with pytest.raises(ValueError, match="Missing or duplicate"):
        check_diagnostics(rows[:-1] + rows[:1], [0, 256, 1024])
    rows[0]["kind"] = "forecast"
    with pytest.raises(ValueError, match="boundary"):
        check_diagnostics(rows, [0, 256, 1024])


@pytest.mark.parametrize("mse,t", [(float("nan"), 100), (-1, 100), (0.1, 1000), (0.1, -1)])
def test_invalid_error_inputs_rejected(mse, t):
    with pytest.raises(ValueError):
        x0_mse_from_epsilon(mse, t)
