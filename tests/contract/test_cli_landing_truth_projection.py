from missionos_cli.job_status import _job_operator_summary


def test_terminal_snapshot_overrides_stale_false_landing_projection() -> None:
    payload = {
        "task": {
            "task_id": "task_landed",
            "status": "blocked",
            "metadata": {},
            "artifacts": {
                "missionos_auto_mission_runtime_snapshot": {
                    "operator_recovery_action": "land",
                    "operator_recovery_command_ack_observed": True,
                    "operator_recovery_command_ack_result": 0,
                    "operator_recovery_assist_low_altitude_force_disarm_ack_result": None,
                    "arming_state": 1,
                    "ground_contact": True,
                    "landed": True,
                    "maybe_landed": True,
                    "monitor_stop_reason": "operator_recovery_dispatch_acked",
                },
                "missionos_auto_mission_runtime_monitor_summary": {
                    "recovery_agent_telemetry_snapshot": {
                        "recovery": {
                            "action": "land",
                            "command_ack_observed": True,
                            "final_landing_safe": False,
                            "recovery_disarm_observed": False,
                            "recovery_latest_ground_confirmed": False,
                            "force_disarm_no_ground_confirmation": False,
                        }
                    },
                    "final_landing_safe": False,
                },
            },
        }
    }

    rendered = "\n".join(_job_operator_summary(payload))

    assert "final_landing_safe=True" in rendered
    assert "disarm_observed=True" in rendered
    assert "latest_ground_confirmed=True" in rendered
    assert "force_disarm_no_ground_confirmation=False" in rendered
