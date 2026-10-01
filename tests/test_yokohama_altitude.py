import ast
import copy
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from scripts.yokohama_altitude import AltitudeCollector, measurements, TOPICS


def fixture():
    raw = {t: "timestamp: 1000000 (0.010000 seconds ago)\n" for t in TOPICS}
    raw[TOPICS[0]] += (
        "z: -15.12\nref_alt: 102\nz_global: true\nref_timestamp: 20\nz_reset_counter: 0\n"
    )
    raw[TOPICS[1]] += "z: -15.11\n"
    raw[TOPICS[2]] += "position: [0, 0, -15.11]\n"
    raw[TOPICS[3]] += "alt: 117.12\n"
    raw[TOPICS[4]] += "alt: 102\nvalid_alt: true\n"
    snap = {"sim_s": 10, "poses": {"x500_0": {"xyz": [0, 0, 15.7167], "age_s": 0.1, "sensor_sim_s": 9.9}}}
    cfg = {
        "run_id": "test",
        "world": {"world_sha256": "x"},
        "flight_stages": [{"name": "HOLD", "target_world_xyz_m": [0, 0, 15]}],
    }
    return raw, snap, cfg


def test_observed_target_is_not_copied_configuration():
    raw, snap, cfg = fixture()
    row = measurements(raw, snap, snap, ("HOLD", "HOLD"), 1, 1.1, cfg)
    assert row["fresh_for_comparison"]
    assert row["controller_setpoint_local_ned_z_m"] == -15.11
    assert row["controller_target_relative_home_altitude_m"] == pytest.approx(15.11)
    assert row["configured_stage_world_z_m"] == 15
    assert row["px4_estimated_relative_home_altitude_m"] == pytest.approx(15.12)
    assert row["gazebo_after"]["xyz"][2] == 15.7167
    assert not row["simultaneous"]


@pytest.mark.parametrize("fault", ["missing", "nan", "wide", "phase", "old_topic", "all_topics_stale", "missing_source_age", "old_gazebo", "future_gazebo"])
def test_missing_or_stale_never_claims_fresh(fault):
    raw, snap, cfg = fixture()
    end = 1.1
    phase = "HOLD"
    if fault == "missing":
        raw[TOPICS[1]] = "timestamp: 1000000 (0.010000 seconds ago)\n"
    if fault == "nan":
        raw[TOPICS[1]] += "z: nan\n"
        raw[TOPICS[1]] = raw[TOPICS[1]].replace("z: -15.11\n", "")
    if fault == "wide":
        end = 5
    if fault == "phase":
        phase = "OTHER"
    if fault == "old_topic":
        raw[TOPICS[2]] = raw[TOPICS[2]].replace("1000000", "4000000")
    if fault == "all_topics_stale":
        raw = {t: value.replace("0.010000 seconds ago", "10.000000 seconds ago") for t, value in raw.items()}
    if fault == "missing_source_age":
        raw[TOPICS[0]] = raw[TOPICS[0]].replace(" (0.010000 seconds ago)", "")
    if fault == "old_gazebo":
        snap["poses"]["x500_0"]["sensor_sim_s"] = 1
    if fault == "future_gazebo":
        snap["poses"]["x500_0"]["sensor_sim_s"] = 11
    row = measurements(raw, snap, snap, ("HOLD", phase), 1, end, cfg)
    assert not row["fresh_for_comparison"]
    if fault in ("missing", "nan"):
        assert row["controller_setpoint_local_ned_z_m"] is None


def test_listener_hang_cache_nonblocking_and_cleanup(tmp_path):
    raw, snap, cfg = fixture()
    listener = tmp_path / "px4-listener"
    listener.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    listener.chmod(0o700)
    collector = AltitudeCollector(
        SimpleNamespace(snapshot=lambda: copy.deepcopy(snap)),
        cfg,
        lambda: "HOLD",
        tmp_path / "diagnostics.jsonl",
        prefix=str(tmp_path / "px4-"),
        timeout=0.1,
    )
    collector.start()
    try:
        deadline = time.monotonic() + 2
        while collector.process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        process = collector.process
        start = time.monotonic()
        for _ in range(100):
            collector.latest()
        assert time.monotonic() - start < 0.1
    finally:
        collector.close()
    assert not collector.thread.is_alive()
    assert collector.process is None
    assert process is not None and process.poll() is not None


def test_cache_age_is_visible():
    raw, snap, cfg = fixture()
    c = AltitudeCollector(None, cfg, lambda: "HOLD", Path("unused"))
    c.cache = measurements(
        raw, snap, snap, ("HOLD", "HOLD"), time.monotonic() - 5, time.monotonic() - 4, cfg
    )
    assert c.latest()["cache_age_host_s"] > 2
    assert not c.latest()["fresh_for_comparison"]


def test_control_sample_remains_free_of_extra_listeners():
    path = Path(__file__).parents[1] / "scripts/yokohama_flight_worker.py"
    tree = ast.parse(path.read_text())
    sample = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "sample"
    )
    text = ast.unparse(sample)
    assert "altitude_collector" not in text
    assert "yokohama_altitude" not in text


@pytest.mark.parametrize(
    "value,valid",
    [("True", True), ("true", True), ("TRUE", True), ("1", True),
     ("False", False), ("false", False), ("FALSE", False), ("0", False),
     ("", False), ("unknown", False), ("trueish", False), ("1.0", False)],
)
def test_listener_boolean_spelling_and_invalid_values(value, valid):
    raw, snap, cfg = fixture()
    raw[TOPICS[0]] = raw[TOPICS[0]].replace("z_global: true", "z_global: " + value)
    raw[TOPICS[4]] = raw[TOPICS[4]].replace("valid_alt: true", "valid_alt: " + value)
    row = measurements(raw, snap, snap, ("HOLD", "HOLD"), 1, 1.1, cfg)
    assert row["fresh_for_comparison"] is valid
    assert (row["px4_estimated_relative_home_altitude_m"] is not None) is valid
    assert (row["controller_target_relative_home_altitude_m"] is not None) is valid


def saved_payload_observation():
    path = Path(__file__).parents[1] / "docs/examples/yokohama-cargo-flight/payload-request.json"
    return json.loads(path.read_text())["observation"]


def test_saved_real_px4_true_reference_recovers_estimate_without_inventing_target():
    observation = saved_payload_observation()
    raw = observation["raw_px4"]
    assert "z_global: True" in raw[TOPICS[0]]
    assert "valid_alt: True" in raw[TOPICS[4]]
    _, _, cfg = fixture()
    snap = {"sim_s": observation["sim_s"], "poses": {"x500_0": observation["vehicle"]}}
    row = measurements(raw, snap, snap, (observation["phase"], observation["phase"]), 1, 1.1, cfg)
    assert row["px4_estimated_relative_home_altitude_m"] == pytest.approx(
        observation["px4_relative_altitude_m"]
    )
    # Saved evidence lacks both actual setpoint topics, so target and freshness stay unavailable.
    assert row["controller_target_relative_home_altitude_m"] is None
    assert not row["fresh_for_comparison"]


def test_saved_px4_reference_with_explicit_target_fixture():
    observation = saved_payload_observation()
    raw = dict(observation["raw_px4"])
    # These two topics and the comparison capture clock are explicit test fixtures,
    # not controller telemetry from the saved flight.
    stamp = 1070712000
    raw[TOPICS[1]] = f"timestamp: {stamp} (0.010000 seconds ago)\nz: -3.125\n"
    raw[TOPICS[2]] = f"timestamp: {stamp} (0.010000 seconds ago)\nposition: [0, 0, -3.125]\n"
    _, _, cfg = fixture()
    snap = {"sim_s": observation["vehicle"]["sensor_sim_s"] + 0.1,
            "poses": {"x500_0": observation["vehicle"]}}
    row = measurements(raw, snap, snap, ("HOLD", "HOLD"), 1, 1.1, cfg)
    assert row["fresh_for_comparison"]
    assert row["px4_estimated_relative_home_altitude_m"] == pytest.approx(2.8237)
    assert row["controller_target_relative_home_altitude_m"] == pytest.approx(3.14873)
    assert row["configured_stage_world_z_m"] == 15


@pytest.mark.parametrize("age", ["-0.001", "-0.010"])
def test_small_negative_source_age_cannot_be_hidden_by_capture_width(age):
    raw,snap,cfg=fixture()
    raw[TOPICS[0]]=raw[TOPICS[0]].replace("0.010000 seconds",age+" seconds")
    row=measurements(raw,snap,snap,("HOLD","HOLD"),1,1.1,cfg)
    assert not row["fresh_for_comparison"]
