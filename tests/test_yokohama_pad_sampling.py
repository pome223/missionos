import copy
import json

import numpy as np
import pytest

from scripts.trace_yokohama_pad_sampling import (
    TraceSampler,
    selected_times,
    sha,
    timestep_map,
    validate,
)
from scripts.evaluate_yokohama_pad_sampling import coverage


def test_history_only_cutoff_and_files(tmp_path):
    stamps = np.arange(16, dtype=np.int64) * 250_000_000
    rgb = np.zeros((16, 224, 224, 3), np.uint8)
    np.savez(tmp_path / "history.npz", rgb=rgb, stamps_ns=stamps)
    p = dict(
        schema="pad_anwm_sampling_trace.v1",
        sampling_steps=[50, 250],
        seeds=[42, 43],
        offset=12,
        training_allowed=False,
        cutoff_stamp_ns=int(stamps[-1]),
        files={"history.npz": sha(tmp_path / "history.npz")},
    )

    def save():
        p["files"]["history.npz"] = sha(tmp_path / "history.npz")
        (tmp_path / "trace-protocol.json").write_text(json.dumps(p))

    save()
    assert validate(tmp_path) == p
    np.savez(tmp_path / "history.npz", rgb=rgb, stamps_ns=stamps, future=rgb[-1])
    save()
    with pytest.raises(ValueError, match="only observations"):
        validate(tmp_path)
    stamps[-1] += 250_000_000
    np.savez(tmp_path / "history.npz", rgb=rgb, stamps_ns=stamps)
    save()
    with pytest.raises(ValueError, match="cutoff"):
        validate(tmp_path)


def test_missing_duplicate_forecasts_and_replay_mismatch_fail():
    p = dict(
        sampling_steps=[50, 250], seeds=[42, 43], display_anchors=[999, 900, 750, 500, 250, 100, 0]
    )
    s = dict(
        status="completed",
        forecast_pairs=4,
        training_updates=0,
        future_input=False,
        runtime_weights_before="a",
        runtime_weights_after="a",
    )
    rows = [
        dict(
            steps=n,
            seed=seed,
            trace_steps=n,
            parity_max_abs=0,
            frames=[dict(original_t=t) for t in selected_times(n, p["display_anchors"])],
            initial_noise_sha256=str(seed),
            conditioning_sha256={"x_cond": "same"},
        )
        for n in p["sampling_steps"]
        for seed in p["seeds"]
    ]
    coverage(p, s, rows)
    for invalid in (rows[:-1], rows[:-1] + rows[:1]):
        with pytest.raises(ValueError, match="Missing or duplicate"):
            coverage(p, s, invalid)
    broken = copy.deepcopy(rows)
    broken[0]["parity_max_abs"] = 0.01
    with pytest.raises(ValueError, match="parity"):
        coverage(p, s, broken)
    broken = copy.deepcopy(rows)
    broken[0]["initial_noise_sha256"] = "different"
    with pytest.raises(ValueError, match="Different initial"):
        coverage(p, s, broken)
    with pytest.raises(ValueError, match="Incomplete"):
        coverage(p, dict(s, status="failed"), rows)


def test_trace_does_not_add_sampler_calls_or_change_output():
    class Value:
        def __init__(self, x):
            self.x = x

        def detach(self):
            return self

        def cpu(self):
            return self

        def clone(self):
            return Value(self.x)

    class Diffusion:
        num_timesteps = 3
        timestep_map = [0, 500, 999]

        def p_sample_loop_progressive(self, model, shape, noise, **kwargs):
            assert noise.x == 17
            assert kwargs["model_kwargs"]["y"].x == 12
            for i in range(3):
                yield {"sample": Value(20 + i), "pred_xstart": Value(50 + i)}

    trace = TraceSampler(Diffusion())
    assert trace.p_sample_loop(None, (1,), Value(17), model_kwargs={"y": Value(12)}).x == 22
    assert [r["x_t"].x for r in trace.records] == [17, 20, 21]
    assert [r["original_t"] for r in trace.records] == [999, 500, 0]
    assert len(timestep_map(250)) == 250
    assert timestep_map(50)[-1] == 999
    with pytest.raises(ValueError, match="Multiple"):
        trace.p_sample_loop(None, (1,), Value(17), model_kwargs={})
