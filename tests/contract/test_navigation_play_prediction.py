"""Play diagnostics select PX4 prediction without fabricating source freshness."""

import json

import pytest

from missionos_core.prediction import prediction_digest
from src.intelligence.mission_assurance_agent import MissionSituation
from src.prediction.navigation import prepare_navigation_prediction
from src.runtime import missionos_play_delivery as delivery
from src.runtime import missionos_play_live_sitl as live
from src.runtime.missionos_play_scenario import load_scenario
from src.runtime.missionos_play_weather import WeatherForecast


@pytest.mark.parametrize("kind", ["delivery", "live_sitl"])
def test_play_required_prediction_reports_missing_source_timestamp(monkeypatch, tmp_path, kind):
    module = delivery if kind == "delivery" else live
    monkeypatch.setattr(module, "start_play_sitl_container", lambda *a, **k: (True, "ready"))
    monkeypatch.setattr(module, "stop_play_sitl_container", lambda **k: None)
    monkeypatch.setenv("MISSIONOS_NAVIGATION_WAM_MODE", "required")
    path = tmp_path / "navigation.json"
    monkeypatch.setenv("MISSIONOS_NAVIGATION_WAM_CONFIG", str(path))
    calls = []

    def graph(**kwargs):
        calls.append(kwargs)
        assert kwargs["navigation_backend"] == "px4"
        telemetry = kwargs["telemetry_snapshot"]
        assert "observed_at" not in telemetry
        path.write_text(
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
                                "model_id": "unused-fixture",
                                "model_sha256": "a" * 64,
                                "mission_contract": "missionos.navigation.px4.v1",
                                "environment_contract": "px4_gazebo_sitl.v1",
                                "input_schema": "missionos_navigation_prediction_input.v1",
                                "policy_sha256": prediction_digest(kwargs["recovery_policy"]),
                            },
                        }
                    },
                }
            )
        )
        situation = MissionSituation(
            situation_id="play-fixture",
            observed_at="2026-09-22T00:00:00+00:00",
            mission_contract={},
            progress={"task_id": kwargs["mission_context"]["task_id"]},
            observations={"runtime_telemetry": telemetry},
            constraints={},
            uncertainty={},
            source_refs=("fixture:play",),
            source_schema_version="fixture.v1",
            input_digest="play-fixture",
            execution_scope="simulator",
        )
        _, record = prepare_navigation_prediction(
            situation,
            backend=kwargs["navigation_backend"],
            policy_sha256=prediction_digest(kwargs["recovery_policy"]),
        )
        assert record["required_blocked"] is True
        assert record["reason"] == "navigation_observation_timestamp_required"
        assert record["invocation_evidence"] is None
        return {
            "graph_runtime_status": "guardrail_blocked",
            "navigation_prediction": record,
            "recovery_result": {"runtime_status": "proposal_guardrail_passed"},
        }

    monkeypatch.setattr(module, "run_missionos_mission_incident_graph", graph)
    scenario = load_scenario()
    forecast = WeatherForecast(
        latitude=scenario.takeoff_lat,
        longitude=scenario.takeoff_lon,
        source_url="fixture://weather",
        provider_response_status="fixture",
        source_unavailable=False,
        captured_at="2026-09-22T00:00:00+00:00",
    )
    if kind == "delivery":
        monkeypatch.setattr(
            module, "upload_mission", lambda *a, **k: {"mission_ack_observed": True}
        )
        monkeypatch.setattr(module, "start_mission", lambda *a, **k: None)
        monkeypatch.setattr(module, "docker_exec_publish_force", lambda *a: lambda *b: None)
        monkeypatch.setattr(module, "resolve_wind_at", lambda *a, **k: (9, 90, {}))
        monkeypatch.setattr(module, "resolve_gust_at", lambda *a, **k: 9)
        monkeypatch.setattr(module, "relative_wind_drag_force", lambda *a, **k: (0, 0))
        clock = iter((0.0, 0.0, 1.0, 20.0))
        result = module.run_play_delivery(
            scenario=scenario,
            forecast=forecast,
            duration_s=10,
            runner=lambda args: live.CommandResult(
                tuple(args), 0, stdout="x: 0\ny: 50\nz: -30\nvx: 0\nvy: 0\n"
            ),
            sleep=lambda _: None,
            clock=lambda: next(clock),
        )
    else:
        monkeypatch.setattr(module, "prepare_and_takeoff", lambda **k: True)
        monkeypatch.setattr(module, "run_wind_driver", lambda **k: ())
        monkeypatch.setattr(module, "_read_local_drift_xy", lambda *a: 2)
        monkeypatch.setattr(module, "_logs", lambda *a: "")
        result = module.run_play_live_sitl(scenario=scenario, forecast=forecast, duration_s=0)
    assert len(calls) == 1
    assert result.status == "blocked"
    assert "mission_incident_graph_guardrail_blocked" in result.blocking_reasons
    assert result.physical_execution_invoked is False
