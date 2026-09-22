"""Real PX4 observation adapter preserves the age of a cached deviation pose."""

import importlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from missionos_core.prediction import prediction_digest
from src.intelligence.mission_assurance_agent import MissionSituation
from src.prediction.navigation import prepare_navigation_prediction


@pytest.mark.parametrize("timestamp_present", [True, False])
def test_cached_deviation_pose_cannot_gain_freshness_from_new_battery_read(
    monkeypatch, tmp_path, timestamp_present
):
    runtime = importlib.import_module("src.runtime.px4_gazebo_route.entrypoint")
    old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    deviation = {"sample": {"x": 1, "y": 0, "z": 2}, "sample_index": 1}
    if timestamp_present:
        deviation["observed_at"] = old
    monkeypatch.setattr(runtime, "WIND_REALISM_SUMMARY", {})
    monkeypatch.setattr(
        runtime,
        "_battery_status_sample",
        lambda: {
            "battery_status_observed": True,
            "battery_remaining_percent": 80,
        },
    )
    monkeypatch.setattr(runtime, "_pose_sample", lambda: {"x": 2, "y": 0, "z": 2})
    monkeypatch.setattr(runtime, "_configured_mission_assurance_context", lambda: {})
    bundles = {}

    def evaluate(**kwargs):
        bundles["original"] = kwargs["telemetry_observer"]("original")
        bundles["current"] = kwargs["telemetry_observer"]("current")
        return bundles

    monkeypatch.setattr(runtime, "_evaluate_live_route_deviation", evaluate)
    runtime._assess_live_route_deviation_mission_assurance(
        task_id="fixture-route",
        run_dir=tmp_path,
        route=SimpleNamespace(),
        pickup_pose={"x": 0, "y": 0, "z": 0},
        route_approval=SimpleNamespace(operator_approval_performed=True),
        deviation=deviation,
        requested_recovery_action="return_to_launch",
        target=None,
    )
    original = bundles["original"]["telemetry_snapshot"]
    assert original["observed_at"] == (old if timestamp_present else None)
    assert original["position"]["local_x_m"] == 1
    current = bundles["current"]["telemetry_snapshot"]
    assert current["position"]["local_x_m"] == 2
    assert datetime.fromisoformat(current["observed_at"]) > datetime.fromisoformat(old)
    assert bundles["original"]["runtime_invocation_evidence"]["invocation_completed_at"] != old

    policy_sha = prediction_digest({"fixture": True})
    config_path = tmp_path / "wam.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "missionos_navigation_wam_config.v1",
                "backends": {
                    "px4": {
                        "endpoint": "http://127.0.0.1:1/predict",
                        "provider_kind": "fixture",
                        "horizon_seconds": 2,
                        "max_age_seconds": 2,
                        "timeout_seconds": 0.1,
                        "binding": {
                            "model_id": "fixture",
                            "model_sha256": "a" * 64,
                            "mission_contract": "missionos.navigation.px4.v1",
                            "environment_contract": "px4_gazebo_sitl.v1",
                            "input_schema": "missionos_navigation_prediction_input.v1",
                            "policy_sha256": policy_sha,
                        },
                    }
                },
            }
        )
    )
    monkeypatch.setenv("MISSIONOS_NAVIGATION_WAM_MODE", "required")
    monkeypatch.setenv("MISSIONOS_NAVIGATION_WAM_CONFIG", str(config_path))
    situation = MissionSituation(
        situation_id="fixture-source-age",
        observed_at=datetime.now(timezone.utc).isoformat(),
        mission_contract={},
        progress={"task_id": "fixture-route"},
        observations={"runtime_telemetry": original},
        constraints={},
        uncertainty={},
        source_refs=("fixture:runtime-pose",),
        source_schema_version="fixture.v1",
        input_digest="fixture-source-age",
        execution_scope="simulator",
    )
    _, record = prepare_navigation_prediction(situation, backend="px4", policy_sha256=policy_sha)
    assert record["required_blocked"] is True
    assert record["reason"] == (
        "stale_or_future_observation"
        if timestamp_present
        else "navigation_observation_timestamp_required"
    )
    assert record["invocation_evidence"] is None
