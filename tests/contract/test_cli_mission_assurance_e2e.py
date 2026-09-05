from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import missionos_cli.cli as missionos_cli
from click.testing import CliRunner


class MissionAssuranceE2EClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def propose_sitl_scenario(self, *, prompt: str) -> dict[str, Any]:
        self.calls.append(("propose", {"prompt": prompt}))
        return {
            "scenario_proposal": {"proposal_id": "proposal_cli_e2e"},
            "validation_result": {"validation_status": "accepted"},
        }

    def approve_sitl_scenario(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("approve", kwargs))
        return {
            "scenario_approval": {"approval_id": "scenario_approval_cli_e2e"},
            "scenario_compile_result": {"compile_id": "compile_cli_e2e"},
            "bounded_simulation_request": {"request_id": "request_cli_e2e"},
        }

    def prepare_sitl_scenario(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("prepare", kwargs))
        return {"summary": {"task_id": "task_cli_e2e"}}

    def start_sitl(self, *, task_id: str) -> dict[str, Any]:
        self.calls.append(("start", {"task_id": task_id}))
        return {"summary": {"task_id": task_id, "readiness_status": "ready"}}

    def execute_sitl(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("execute", kwargs))
        e2e = {
            "full_gateway_runtime_loop": True,
            "e2e_status": "completed",
        }
        return {
            "task": {
                "task_id": "task_cli_e2e",
                "status": "completed",
                "artifacts": {
                    "missionos_mission_assurance_gateway_px4_e2e": e2e
                },
            },
            "summary": {"full_gateway_runtime_loop": True},
            "missionos_mission_assurance_gateway_px4_e2e": e2e,
        }


def test_mission_assurance_e2e_command_uses_cli_gateway_task_chain(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    client = MissionAssuranceE2EClient()
    monkeypatch.setattr(missionos_cli, "make_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(missionos_cli, "_ensure_gateway", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--json-output",
            "--state-path",
            str(tmp_path / "state.json"),
            "mission-assurance-e2e",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["cli_entrypoint_observed"] is True
    assert payload["task_id"] == "task_cli_e2e"
    assert payload["mission_assurance_gateway_px4_e2e"][
        "full_gateway_runtime_loop"
    ] is True
    assert [name for name, _payload in client.calls] == [
        "propose",
        "approve",
        "prepare",
        "start",
        "execute",
    ]
    assert client.calls[-1][1] == {
        "task_id": "task_cli_e2e",
        "live_flight_mode": True,
        "mission_assurance_on_deviation": True,
    }


def test_mission_assurance_e2e_requires_explicit_live_approval(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    client = MissionAssuranceE2EClient()
    monkeypatch.setattr(missionos_cli, "make_client", lambda *_args, **_kwargs: client)

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "mission-assurance-e2e",
        ],
    )

    assert result.exit_code != 0
    assert "--yes is required" in result.output
    assert client.calls == []


def test_mission_assurance_e2e_exits_nonzero_when_gateway_loop_is_blocked(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class BlockedMissionAssuranceE2EClient(MissionAssuranceE2EClient):
        def execute_sitl(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("execute", kwargs))
            e2e = {
                "full_gateway_runtime_loop": False,
                "e2e_status": "blocked",
                "blocking_reasons": ["mission_assurance_model_inference_not_observed"],
            }
            return {
                "task": {
                    "task_id": "task_cli_e2e",
                    "status": "failed",
                    "artifacts": {
                        "missionos_mission_assurance_gateway_px4_e2e": e2e
                    },
                },
                "summary": {"full_gateway_runtime_loop": False},
                "missionos_mission_assurance_gateway_px4_e2e": e2e,
            }

    client = BlockedMissionAssuranceE2EClient()
    monkeypatch.setattr(missionos_cli, "make_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(missionos_cli, "_ensure_gateway", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        missionos_cli.missionos,
        [
            "--state-path",
            str(tmp_path / "state.json"),
            "mission-assurance-e2e",
            "--yes",
        ],
    )

    assert result.exit_code != 0
    assert "did not satisfy the full Gateway/PX4 loop" in result.output
    assert "mission_assurance_model_inference_not_observed" in result.output


def test_mission_assurance_polling_does_not_treat_upload_completion_as_e2e_end() -> None:
    class TransientUploadCompleteClient:
        def execute_sitl(self, **_kwargs: Any) -> dict[str, Any]:
            time.sleep(0.08)
            return {
                "summary": {"full_gateway_runtime_loop": True},
                "missionos_mission_assurance_gateway_px4_e2e": {
                    "full_gateway_runtime_loop": True
                },
            }

        def get(self, path: str) -> dict[str, Any]:
            if path.endswith("/timeline?limit=5"):
                return {"entries": []}
            return {
                "task_id": "task_transient_upload_complete",
                "status": "completed",
                "artifacts": {
                    "px4_gazebo_sitl_mission_upload_receipt": {
                        "upload_status": "uploaded"
                    }
                },
            }

    payload, task, _timeline = missionos_cli._execute_sitl_with_task_polling(
        TransientUploadCompleteClient(),
        task_id="task_transient_upload_complete",
        live_flight_mode=True,
        mission_assurance_on_deviation=True,
        poll_interval=0.01,
    )

    assert payload is not None
    assert payload["summary"]["full_gateway_runtime_loop"] is True
    assert task is not None
    assert task["status"] == "completed"


def test_live_polling_dispatches_one_approved_preflight_calibration() -> None:
    class PreflightCalibrationClient:
        def __init__(self) -> None:
            self.dispatches: list[dict[str, Any]] = []

        def execute_sitl(self, **_kwargs: Any) -> dict[str, Any]:
            time.sleep(0.08)
            return {"summary": {"task_id": "task_preflight", "task_status": "running"}}

        def get(self, path: str) -> dict[str, Any]:
            if "/timeline?" in path:
                return {"entries": []}
            return {
                "task": {
                    "task_id": "task_preflight",
                    "status": "running",
                    "artifacts": {
                        "missionos_auto_mission_runtime_snapshot": {
                            "snapshot_status": "running",
                            "heartbeat_observed": True,
                            "landed": False,
                            "nav_state": 3,
                            "local_x_m": 12.0,
                            "local_y_m": 4.0,
                            "altitude_above_home_m": 8.0,
                            "distance_to_home_m": 12.7,
                            "wind_speed_mps": 3.0,
                            "terrain_clearance_m": 29.8,
                            "terrain_clearance_target_m": 30.0,
                            "terrain_clearance_grace_m": 1.0,
                            "operator_recovery_request_observed": False,
                        }
                    },
                }
            }

        def recovery_dispatch(self, **kwargs: Any) -> dict[str, Any]:
            self.dispatches.append(dict(kwargs))
            return {
                "summary": {
                    "task_id": kwargs["task_id"],
                    "recovery_action": kwargs["recovery_action"],
                    "active_runner_request_queued": True,
                }
            }

    client = PreflightCalibrationClient()
    observed: list[dict[str, Any]] = []

    payload, _task, _timeline = missionos_cli._execute_sitl_with_task_polling(
        client,
        task_id="task_preflight",
        live_flight_mode=True,
        preflight_calibration_approved=True,
        poll_interval=0.01,
        preflight_calibration_callback=observed.append,
    )

    assert payload is not None
    assert client.dispatches == [
        {
            "task_id": "task_preflight",
            "recovery_action": "calibrate_offboard",
            "recovery_parameters": {
                "target_x_m": 8.838,
                "target_y_m": 13.487,
                "target_altitude_m": 8.7,
                "calibration_only": True,
                "resume_original_route": True,
            },
        }
    ]
    assert len(observed) == 1
