"""Training payloads must not smuggle future observations into native requests."""


import numpy as np
import pytest

from scripts.yokohama_pad_focus_data import CROP, crop, sha, validate, write
from scripts.capture_yokohama_pad_motion import native_focus_cases


def dataset(tmp_path):
    (tmp_path / "histories").mkdir()
    stamps = np.arange(16, dtype=np.int64) * 250000000
    np.savez_compressed(
        tmp_path / "histories/test.npz", rgb=np.zeros((16, 224, 224, 3), np.uint8), stamps_ns=stamps
    )
    d = dict(
        schema="pad_native_focus_dataset.v1",
        crop_xyxy=list(CROP),
        test_targets_uploaded=False,
        samples=[
            dict(
                id="test",
                split="test",
                history="histories/test.npz",
                offset=16,
                cutoff_stamp_ns=int(stamps[-1]),
            )
        ],
        assets={"histories/test.npz": sha(tmp_path / "histories/test.npz")},
    )
    write(tmp_path / "dataset.json", d)
    return d


def test_past_only_payload_is_cpu_readable(tmp_path):
    dataset(tmp_path)
    assert validate(tmp_path)["samples"][0]["split"] == "test"


@pytest.mark.parametrize(
    "field,value",
    [("target", "future.png"), ("truth", "occupied"), ("offset", 64), ("cutoff_stamp_ns", 0)],
)
def test_reject_future_supervision_or_wrong_clock(tmp_path, field, value):
    d = dataset(tmp_path)
    d["samples"][0][field] = value
    write(tmp_path / "dataset.json", d)
    with pytest.raises(ValueError):
        validate(tmp_path)


def test_reject_unlisted_future_file(tmp_path):
    dataset(tmp_path)
    (tmp_path / "future.png").write_bytes(b"not an observation")
    with pytest.raises(ValueError, match="payload"):
        validate(tmp_path)


def test_reject_actor_pose_in_history(tmp_path):
    d = dataset(tmp_path)
    np.savez_compressed(
        tmp_path / "histories/test.npz",
        rgb=np.zeros((16, 224, 224, 3), np.uint8),
        stamps_ns=np.arange(16) * 250000000,
        lead_pose=np.zeros((16, 3)),
    )
    d["assets"]["histories/test.npz"] = sha(tmp_path / "histories/test.npz")
    write(tmp_path / "dataset.json", d)
    with pytest.raises(ValueError, match="observation"):
        validate(tmp_path)


def test_new_captures_have_complete_future_windows():
    cases = native_focus_cases()
    assert len({c["id"] for c in cases}) == 3
    assert sum(len(c["cutoffs"]) for c in cases) == 14
    assert all(4 <= t <= c["knots"][-1][0] - 4 for c in cases for t in c["cutoffs"])


def test_fixed_crop_uses_recorded_pixels():
    a = np.full((360, 640, 3), 73, np.uint8)
    result = crop(a)
    assert result.shape == (224, 224, 3) and np.all(result == 73)
