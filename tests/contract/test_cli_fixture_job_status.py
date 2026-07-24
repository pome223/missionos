from missionos_cli import cli as missionos_cli
from missionos_gateway.server import _fixture_task


def test_fixture_job_status_does_not_claim_actual_sitl_flight() -> None:
    lines = missionos_cli._job_operator_summary(_fixture_task("task_fixture"))

    rendered = "\n".join(lines)

    assert "Fixture Only: no dispatch or live SITL flight was invoked" in rendered
    assert "Route: [----------------------------] 0.0%" in rendered
    assert "Distance: 0 m" in rendered
    assert "actual_sitl_flight=False" in rendered
    assert "physical_execution=False" in rendered


def test_px4_terminal_status_separates_historical_hold_from_later_dispatch() -> None:
    payload = _fixture_task("task_terminal_recovery")
    task = payload["task"]
    task["status"] = "completed"
    artifacts = task["artifacts"]
    artifacts["missionos_auto_mission_runtime_snapshot"] = {
        "landed": True,
        "arming_state": 1,
        "operator_recovery_request_observed": True,
        "operator_recovery_command_ack_observed": True,
        "operator_recovery_command_ack_result": "ACCEPTED",
        "terrain_clearance_m": 0.0,
        "terrain_clearance_target_m": 30.0,
        "terrain_clearance_margin_m": -30.0,
        "terrain_clearance_status": "below_minimum",
    }
    artifacts["missionos_runtime_recovery_safety_hold_receipt"] = {
        "request_status": "observed"
    }
    artifacts["missionos_runtime_recovery_dispatch_receipt"] = {
        "dispatch_status": "queued_for_active_runner",
        "recovery_action": "avoid_obstacle",
        "active_runner_request_queued": True,
    }

    rendered = "\n".join(missionos_cli._job_operator_summary(payload))

    assert "at_hold_recovery_dispatch=false" in rendered
    assert "later dispatch is reported separately" in rendered
    assert "Operator Dispatch: status=queued_for_active_runner" in rendered
    assert "Terrain: AGL=0 m; target=30 m; margin=-30 m; status=landed_not_applicable" in rendered
