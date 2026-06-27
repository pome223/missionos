"""Runtime smoke for Mission Designer SITL dropoff verification.

This smoke exercises the Mission Designer Gateway prepare/execute path, then
attaches already-observed PX4/Gazebo horizontal-route facts to the persisted
task chain. The horizontal summary must come from the opt-in real SITL
horizontal-route smoke; this script does not synthesize flight, payload-release,
or dropoff facts.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from fastapi.testclient import TestClient

from src.runtime.px4_gazebo_mission_designer_sitl_runner import (
    MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV,
    attach_px4_gazebo_mission_designer_sitl_dropoff_verification,
    attach_px4_gazebo_mission_designer_sitl_flight_evidence,
    attach_px4_gazebo_mission_designer_sitl_payload_release_observation,
)
from src.runtime.px4_gazebo_mission_designer_sitl_live_flight_run import (
    attach_px4_gazebo_mission_designer_sitl_live_flight_run,
    stamp_mission_designer_live_sitl_horizontal_summary,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_MISSION_ACCEPTED,
    PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
    PX4GazeboSITLMissionUploader,
)

OPT_IN_ENV = "RUN_MISSION_DESIGNER_SITL_DROPOFF_VERIFICATION_SMOKE"
SUMMARY_PATH_ENV = "MISSION_DESIGNER_SITL_DROPOFF_VERIFICATION_SUMMARY_PATH"


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run this smoke.")


def _load_horizontal_summary() -> dict[str, Any]:
    summary_path_value = os.getenv(SUMMARY_PATH_ENV, "").strip()
    if not summary_path_value:
        raise SystemExit(f"Set {SUMMARY_PATH_ENV}=<horizontal summary.json>.")
    summary_path = Path(summary_path_value).expanduser()
    if not summary_path.is_file():
        raise SystemExit(f"{SUMMARY_PATH_ENV} must point to summary.json.")
    summary = json.loads(summary_path.read_text())
    required_true = (
        "actual_px4_gazebo_horizontal_smoke_observed",
        "preupload_mission_performed",
        "preupload_mission_ack_observed",
        "dropoff_region_reached",
        "payload_release_observed",
    )
    for key in required_true:
        if summary.get(key) is not True:
            raise RuntimeError(f"dropoff verification smoke requires {key}=true")
    if summary.get("preupload_mission_ack_type") != MAV_MISSION_ACCEPTED:
        raise RuntimeError("dropoff verification smoke requires accepted upload ACK")
    if (
        summary.get("payload_release_event_source")
        != "gazebo_detachable_joint_detach_event"
    ):
        raise RuntimeError(
            "dropoff verification smoke requires Gazebo detachable-joint event"
        )
    for key in (
        "completed_pose_xy_m",
        "completed_pose_z_m",
        "route_target_x_m",
        "route_target_y_m",
        "recorded_at",
        "payload_release_observed_at",
        "payload_release_position_x_m",
        "payload_release_position_y_m",
        "payload_release_position_z_m",
    ):
        if summary.get(key) in ("", None):
            raise RuntimeError(f"dropoff verification smoke requires {key}")
    return summary


def _configure_temp_paths(tmp: Path) -> None:
    os.environ["TASK_STORE_DB_PATH"] = str(tmp / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(tmp / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(tmp / "computer_trajectories.db")
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(
        tmp / "physical_ai_validation.db"
    )
    os.environ[MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV] = "1"


@contextmanager
def _observed_uploader(horizontal_summary: dict[str, Any]):
    original_upload = PX4GazeboSITLMissionUploader.upload
    sequences = tuple(
        int(item) for item in horizontal_summary["preupload_mission_request_sequences"]
    )

    def _upload(
        self,
        *,
        items,
        target_endpoint: str,
        timeout_seconds: float,
    ):
        if target_endpoint != PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT:
            raise RuntimeError(f"unexpected SITL upload endpoint: {target_endpoint}")
        if timeout_seconds <= 0:
            raise RuntimeError("timeout_seconds must be positive")
        if len(items) != len(sequences):
            raise RuntimeError("observed mission request sequence count mismatch")
        return sequences, MAV_MISSION_ACCEPTED

    PX4GazeboSITLMissionUploader.upload = _upload
    try:
        yield
    finally:
        PX4GazeboSITLMissionUploader.upload = original_upload


def _post_json(
    client: TestClient, path: str, payload: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(path, json=payload)
    response.raise_for_status()
    return response.json()


def _prepare_and_execute_task(
    client: TestClient, *, owner_session_id: str
) -> dict[str, Any]:
    proposed = _post_json(
        client,
        "/px4-gazebo/mission-scenarios/propose",
        {
            "prompt": (
                "３０００メートルの山の山頂に重さ５キロの水を"
                "届けるミッションを作成して"
            )
        },
    )
    approved = _post_json(
        client,
        "/px4-gazebo/mission-scenarios/approve",
        {
            "scenario_proposal": proposed["scenario_proposal"],
            "validation_result": proposed["validation_result"],
        },
    )
    prepared = _post_json(
        client,
        "/px4-gazebo/mission-scenarios/prepare-sitl-execution",
        {
            "scenario_proposal": proposed["scenario_proposal"],
            "validation_result": proposed["validation_result"],
            "scenario_approval": approved["scenario_approval"],
            "scenario_compile_result": approved["scenario_compile_result"],
            "bounded_simulation_request": approved["bounded_simulation_request"],
            "owner_session_id": owner_session_id,
            "owner_user_id": "operator",
        },
    )
    executed = _post_json(
        client,
        "/px4-gazebo/mission-scenarios/execute-sitl",
        {
            "task_id": prepared["summary"]["task_id"],
            "explicit_execution_approval": True,
        },
    )
    if executed["summary"]["upload_status"] != "uploaded":
        raise RuntimeError("Gateway execute route did not observe upload")
    return prepared


def _attach_same_run_live_flight(
    gateway,
    task_id: str,
    horizontal_summary: dict[str, Any],
) -> dict[str, Any]:
    task = gateway.task_store.get(task_id)
    if task is None:
        raise RuntimeError(f"task {task_id} missing before live flight attach")
    live_summary = stamp_mission_designer_live_sitl_horizontal_summary(
        task=task,
        horizontal_summary=horizontal_summary,
    )
    attach_px4_gazebo_mission_designer_sitl_live_flight_run(
        task_id,
        horizontal_summary=live_summary,
        task_store_factory=lambda: gateway.task_store,
    )
    return live_summary


def _main() -> dict[str, Any]:
    _require_opt_in()
    horizontal_summary = _load_horizontal_summary()
    previous_runner_opt_in = os.environ.get(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV)
    try:
        with tempfile.TemporaryDirectory(
            prefix="mission-designer-sitl-dropoff-"
        ) as tmp:
            _configure_temp_paths(Path(tmp))

            from src.config.settings import reset_settings
            from src.gateway.server import create_gateway
            from src.runtime.task_store import reset_task_store

            reset_settings()
            reset_task_store()
            gateway = create_gateway()
            with (
                _observed_uploader(horizontal_summary),
                TestClient(gateway.app) as client,
            ):
                prepared = _prepare_and_execute_task(
                    client,
                    owner_session_id="smoke-sitl-dropoff-verification",
                )
                task_id = prepared["summary"]["task_id"]
                live_summary = _attach_same_run_live_flight(
                    gateway,
                    task_id,
                    horizontal_summary,
                )
                attach_px4_gazebo_mission_designer_sitl_flight_evidence(
                    task_id,
                    horizontal_summary=live_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                attach_px4_gazebo_mission_designer_sitl_payload_release_observation(
                    task_id,
                    horizontal_summary=live_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                dropoff_attached = (
                    attach_px4_gazebo_mission_designer_sitl_dropoff_verification(
                        task_id,
                        horizontal_summary=live_summary,
                        task_store_factory=lambda: gateway.task_store,
                    )
                )
                negative_prepared = _prepare_and_execute_task(
                    client,
                    owner_session_id="smoke-sitl-dropoff-negative",
                )
                negative_task_id = negative_prepared["summary"]["task_id"]
                negative_summary = _attach_same_run_live_flight(
                    gateway,
                    negative_task_id,
                    horizontal_summary,
                )
                attach_px4_gazebo_mission_designer_sitl_flight_evidence(
                    negative_task_id,
                    horizontal_summary=negative_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                missing_payload_rejected = False
                try:
                    attach_px4_gazebo_mission_designer_sitl_dropoff_verification(
                        negative_task_id,
                        horizontal_summary=negative_summary,
                        task_store_factory=lambda: gateway.task_store,
                    )
                except Exception as exc:
                    missing_payload_rejected = "payload_release_observation" in str(exc)
                stored_response = client.get(f"/tasks/{task_id}")
                stored_response.raise_for_status()
                stored = stored_response.json()["task"]

        mission_designer_verification = dropoff_attached[
            "px4_gazebo_mission_designer_sitl_dropoff_verification"
        ]
        sitl_verification = dropoff_attached["px4_gazebo_sitl_dropoff_verification"]
        live_run = stored["artifacts"][
            "px4_gazebo_mission_designer_sitl_live_flight_run"
        ]
        live_run_ref = (
            "px4_gazebo_mission_designer_sitl_live_flight_run:"
            + live_run["live_flight_run_id"]
        )
        if missing_payload_rejected is not True:
            raise RuntimeError("missing payload release branch was not rejected")
        if stored["status"] != "completed":
            raise RuntimeError("stored task did not remain completed")
        for key in (
            "px4_gazebo_sitl_dropoff_flight_fact",
            "px4_gazebo_sitl_dropoff_verification",
            "px4_gazebo_mission_designer_sitl_dropoff_verification",
        ):
            if key not in stored["artifacts"]:
                raise RuntimeError(f"stored task missing {key}")
        if mission_designer_verification["dropoff_verified"] is not True:
            raise RuntimeError("Mission Designer dropoff verification did not pass")
        if sitl_verification["dropoff_verified"] is not True:
            raise RuntimeError("SITL dropoff verifier did not pass")
        if mission_designer_verification["live_flight_run_ref"] != live_run_ref:
            raise RuntimeError("dropoff verification live-run ref mismatch")
        if mission_designer_verification["mission_item_binding_sha256"] != (
            live_run["mission_item_binding_sha256"]
        ):
            raise RuntimeError("dropoff verification mission item binding mismatch")
        if (
            mission_designer_verification["dropoff_verification_bound_to_live_run"]
            is not True
        ):
            raise RuntimeError("dropoff verification not bound to live run")

        return {
            "mission_designer_sitl_dropoff_verification_smoke_passed": True,
            "task_id": stored["task_id"],
            "task_status": stored["status"],
            "mission_designer_dropoff_verification_schema_version": (
                mission_designer_verification["schema_version"]
            ),
            "mission_designer_dropoff_verification_ref": (
                "px4_gazebo_mission_designer_sitl_dropoff_verification:"
                f"{mission_designer_verification['verification_id']}"
            ),
            "dropoff_verification_ref": (
                mission_designer_verification["sitl_dropoff_verification_ref"]
            ),
            "live_flight_run_ref": live_run_ref,
            "dropoff_verification_bound_to_live_run": (
                mission_designer_verification["dropoff_verification_bound_to_live_run"]
            ),
            "mission_item_binding_sha256": mission_designer_verification[
                "mission_item_binding_sha256"
            ],
            "dropoff_verified": mission_designer_verification["dropoff_verified"],
            "predicate_mode": mission_designer_verification["predicate_mode"],
            "observed_distance_to_dropoff_m": (
                mission_designer_verification["observed_distance_to_dropoff_m"]
            ),
            "release_distance_to_dropoff_m": (
                mission_designer_verification["release_distance_to_dropoff_m"]
            ),
            "release_time_delta_seconds": (
                mission_designer_verification["release_time_delta_seconds"]
            ),
            "missing_payload_release_rejected": missing_payload_rejected,
            "horizontal_route_artifact_dir": horizontal_summary["artifact_dir"],
            "hardware_target_allowed": mission_designer_verification[
                "hardware_target_allowed"
            ],
            "physical_execution_invoked": mission_designer_verification[
                "physical_execution_invoked"
            ],
            "ros_dispatch_performed": mission_designer_verification[
                "ros_dispatch_performed"
            ],
            "actuator_execution_performed": mission_designer_verification[
                "actuator_execution_performed"
            ],
            "synthetic_success_allowed": mission_designer_verification[
                "synthetic_success_allowed"
            ],
        }
    finally:
        if previous_runner_opt_in is None:
            os.environ.pop(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV, None)
        else:
            os.environ[MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV] = (
                previous_runner_opt_in
            )


if __name__ == "__main__":
    smoke_summary = _main()
    print(json.dumps(smoke_summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(smoke_summary, sort_keys=True))
