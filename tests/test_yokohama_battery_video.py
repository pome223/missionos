import pytest
from scripts.build_yokohama_battery_video import align_battery, battery_samples


def row(raw=None):
    return dict(
        run_id="run",
        world_sha256="world",
        sim_s=10,
        phase="hold",
        raw_px4=dict(
            battery_status=raw
            or "timestamp: 10000000\nconnected: True\nremaining: 0.8\nvoltage_v: 16\ncurrent_a: -1\n"
        ),
    )


def test_timestamp_alignment_never_uses_future_or_stale_values():
    samples = battery_samples([row()], "run", "world")
    assert not align_battery(samples, 9)["available"]
    assert align_battery(samples, 10)["remaining_fraction"] == 0.8
    assert align_battery(samples, 12)["available"]
    assert not align_battery(samples, 12.001)["available"]


@pytest.mark.parametrize(
    "raw", ["", "remaining: nan\n", "remaining: 1.1\n", "connected: False\nremaining: 0.8\n"]
)
def test_missing_or_invalid_is_not_a_zero_or_carried_reading(raw):
    samples = battery_samples([row("timestamp: 10000000\nvoltage_v: 16\n" + raw)], "run", "world")
    assert not align_battery(samples, 10)["available"]


def test_foreign_run_or_clock_drift_rejected():
    with pytest.raises(ValueError, match="Foreign"):
        battery_samples([row()], "other", "world")
    r = row()
    r["sim_s"] = 20
    with pytest.raises(ValueError, match="clocks"):
        battery_samples([r], "run", "world")
