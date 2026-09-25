"""Production graph/HTTP composition checks, with explicitly synthetic model IO."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.smoke_navigation_wam_jev import (
    FAILURE_CONDITIONS,
    assert_no_authority,
    navigation_fixture,
    run_case,
    verify_case,
)


@pytest.mark.parametrize("backend", ["px4", "nav2"])
@pytest.mark.parametrize("wam_mode", ["off", "shadow", "required"])
@pytest.mark.parametrize("jev_mode", ["off", "shadow", "primary"])
def test_navigation_wam_and_jev_modes(tmp_path, backend, wam_mode, jev_mode):
    arguments = dict(backend=backend, wam_mode=wam_mode, jev_mode=jev_mode)
    verify_case(run_case(tmp_path, **arguments), **arguments)


@pytest.mark.parametrize("backend", ["px4", "nav2"])
@pytest.mark.parametrize("condition", FAILURE_CONDITIONS)
def test_required_prediction_failure_blocks_before_judgment(tmp_path, backend, condition):
    arguments = dict(backend=backend, wam_mode="required", jev_mode="primary", condition=condition)
    verify_case(run_case(tmp_path, **arguments), **arguments)


@pytest.mark.parametrize("backend", ["px4", "nav2"])
@pytest.mark.parametrize("condition", FAILURE_CONDITIONS)
def test_shadow_prediction_failure_cannot_block_or_change_judgment(tmp_path, backend, condition):
    arguments = dict(backend=backend, wam_mode="shadow", jev_mode="primary", condition=condition)
    verify_case(run_case(tmp_path, **arguments), **arguments)


@pytest.mark.parametrize("backend", ["px4", "nav2"])
@pytest.mark.parametrize("jev_mode", ["shadow", "primary"])
def test_jev_failure_only_blocks_primary(tmp_path, backend, jev_mode):
    arguments = dict(backend=backend, wam_mode="required", jev_mode=jev_mode, condition="jev_failure")
    verify_case(run_case(tmp_path, **arguments), **arguments)


def _existing_fixture_module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_turtlebot3_checkpoint_reaches_navigation_http_and_jev(tmp_path):
    from src.runtime.turtlebot3_mission_incident import (
        judge_turtlebot3_checkpoint,
        navigation_telemetry_from_evaluation,
    )

    case = _existing_fixture_module("test_turtlebot3_mission_incident").input_case()
    resolution = case["obstacle"]["recovery_candidate_resolution"]
    resolution["navigation_telemetry"] = navigation_telemetry_from_evaluation(
        {
            "observation_captured_at": datetime.now(timezone.utc).isoformat(),
            "global_costmap_snapshot_hash": "fixture-global-costmap",
            "local_costmap_snapshot_hash": "fixture-local-costmap",
            "global_costmap_content_sha256": "a" * 64,
            "local_costmap_content_sha256": "b" * 64,
        },
        candidates=[{
            "candidate_id": "fixture-path", "x_m": 2.0, "y_m": 1.0,
            "yaw_rad": 0.0,
            "path_points": [{"x_m": 0.0, "y_m": 0.0}, {"x_m": 2.0, "y_m": 1.0}],
            "path_sha256": "fixture-path-digest",
        }],
    )
    with navigation_fixture(tmp_path, backend="nav2", policy={}) as fixture:
        graph = judge_turtlebot3_checkpoint(**case)
    assert fixture["calls"] == {"wam": 1, "primary": 0, "jev": 1}
    assert graph["prediction_admission"]["status"] == "adopted"
    assert graph["mission_assurance_response_kind"] == "hold"
    assert graph["dispatch_prevented_by_mission_assurance"] is True
    assert_no_authority(graph)


def test_native_px4_deviation_reaches_navigation_http_and_jev(tmp_path):
    from src.runtime.px4_gazebo_route.live_mission_assurance import (
        evaluate_live_route_deviation,
        horizontal_route_mission_assurance_policy,
    )

    fixture_module = _existing_fixture_module("test_px4_live_mission_assurance")
    route = fixture_module._route()
    policy = horizontal_route_mission_assurance_policy(route)
    with navigation_fixture(tmp_path, backend="px4", policy=policy) as fixture:
        result = evaluate_live_route_deviation(
            task_id="navigation-wam-fixture",
            artifact_dir=tmp_path / "px4-artifacts",
            route=route,
            deviation=fixture_module._deviation(),
            available_recovery_executor_action="rtl",
            operator_preapproval_observed=False,
            telemetry_observer=fixture_module._observer(),
            recovery_agent_runner=lambda **_: fixture_module._recovery_result(),
        )
    graph = result["missionos_mission_incident_graph"]
    assert fixture["calls"] == {"wam": 1, "primary": 0, "jev": 1}
    assert graph["prediction_admission"]["status"] == "adopted"
    assert graph["mission_assurance_response_kind"] == "hold"
    assert graph["dispatch_prevented_by_mission_assurance"] is True
    assert_no_authority(graph)
