import pytest
from scripts.build_yokohama_battery_video import align_battery, align_wind, battery_samples


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


def test_wind_video_never_uses_future_or_unconfirmed_gust():
    r = dict(
        run_id="run",
        world_sha256="world",
        confirmed=True,
        end_sim_s=10,
        requested_enu_mps=[7.5, 0, 0],
        gust_id=0,
        zone="offshore",
        sequence=1,
    )
    assert align_wind([r], 9, "run", "world")["gust_id"] is None
    assert align_wind([r], 10, "run", "world")["gust_id"] == 0
    r["confirmed"] = False
    assert "UNCONFIRMED" in align_wind([r], 10, "run", "world")["label"]
    with pytest.raises(ValueError, match="Foreign"):
        align_wind([r], 10, "other", "world")


def test_recovery_caption_is_not_backdated_or_invented_after_last_sample():
    from scripts.build_yokohama_battery_video import recovery_timeline, align_recovery

    rows = [dict(wall_s=1, sim_s=10), dict(wall_s=2, sim_s=11), dict(wall_s=3, sim_s=12)]
    events = [
        dict(event="city_attempt_revoked", wall_s=1.5),
        dict(event="city_recovery_held", wall_s=2.5),
        dict(event="city_segment_arrived", wall_s=3.5),
    ]
    timeline = recovery_timeline(events, rows)
    assert len(timeline) == 2
    assert "not observed" in align_recovery(timeline, 10.9)["label"]
    assert "INTERRUPTED" in align_recovery(timeline, 11)["label"]
    assert "STABLE" in align_recovery(timeline, 12)["label"]
