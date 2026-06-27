"""Epic-exit smoke for Mission Designer SITL delivery completion.

The smoke exercises the Mission Designer Gateway prepare/execute path, then
binds a previously observed PX4/Gazebo horizontal-route summary into the same
task chain as flight, payload-release, dropoff, and epic-exit artifacts. The
summary must come from the opt-in real SITL horizontal-route smoke; this script
does not synthesize flight, payload-release, or dropoff facts.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from fastapi.testclient import TestClient

from scripts import smoke_px4_gazebo_sitl_mission_upload as sitl_upload_smoke
from src.runtime.px4_gazebo_mission_designer_sitl_delivery_epic_exit import (
    attach_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result,
)
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

OPT_IN_ENV = "RUN_MISSION_DESIGNER_SITL_DELIVERY_EPIC_EXIT_SMOKE"
SUMMARY_PATH_ENV = "MISSION_DESIGNER_SITL_DELIVERY_EPIC_EXIT_SUMMARY_PATH"
PROMPT = "３０００メートルの山の山頂に重さ５キロの水を届けるミッションを作成して"


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
            raise RuntimeError(f"epic-exit smoke requires {key}=true")
    if summary.get("preupload_mission_ack_type") != MAV_MISSION_ACCEPTED:
        raise RuntimeError("epic-exit smoke requires accepted upload ACK")
    if tuple(summary.get("preupload_mission_request_sequences") or ()) != (0, 1, 2, 3):
        raise RuntimeError("epic-exit smoke requires mission request sequence 0..3")
    if (
        summary.get("payload_release_event_source")
        != "gazebo_detachable_joint_detach_event"
    ):
        raise RuntimeError(
            "epic-exit smoke requires Gazebo detachable-joint payload release"
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
            raise RuntimeError(f"epic-exit smoke requires {key}")
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
def _docker_exec_sitl_uploader():
    """Route the Gateway uploader boundary into the live SITL container.

    Docker Desktop does not expose the PX4 MAVLink UDP endpoint to the host in
    this smoke. The replacement still performs a real upload: the Gateway route
    invokes this uploader, which executes the established MAVLink upload script
    inside the running PX4/Gazebo SITL container and returns its observed request
    sequence and ACK.
    """

    original_upload = PX4GazeboSITLMissionUploader.upload
    upload_attempts = 0

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
        nonlocal upload_attempts
        if upload_attempts:
            sitl_upload_smoke._stop_container()
            sitl_upload_smoke._start_container()
        upload_attempts += 1
        expected_upload_items = sitl_upload_smoke._mission_upload_item_tuples(items)
        observed_upload = sitl_upload_smoke._actual_upload(items=items)
        observed_items = tuple(
            tuple(item) for item in (observed_upload.get("mission_items") or ())
        )
        if observed_items != expected_upload_items:
            raise RuntimeError("Docker/PX4/Gazebo upload item binding mismatch")
        request_sequences = tuple(
            int(item) for item in observed_upload["mission_request_sequences"]
        )
        if len(items) != len(request_sequences):
            raise RuntimeError("observed mission request sequence count mismatch")
        if observed_upload.get("mission_ack_observed") is not True:
            raise RuntimeError("Docker/PX4/Gazebo upload did not observe ACK")
        return request_sequences, int(observed_upload["mission_ack_type"])

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
        {"prompt": PROMPT},
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


def _epic_exit_rejected(client: TestClient, task_id: str, expected: str) -> bool:
    try:
        task_response = client.get(f"/tasks/{task_id}")
        task_response.raise_for_status()
        attach_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result(
            task_id,
            prompt=PROMPT,
            upload_only_delivery_success_rejected=True,
            missing_flight_delivery_success_rejected=True,
            missing_payload_release_delivery_success_rejected=True,
            missing_dropoff_delivery_success_rejected=True,
            task_store_factory=lambda: client.app.state.gateway.task_store,
        )
    except Exception as exc:
        return expected in str(exc)
    return False


def _attach_same_run_live_flight(
    gateway,
    task_id: str,
    horizontal_summary: dict[str, Any],
) -> dict[str, Any]:
    task = gateway.task_store.get(task_id)
    if task is None:
        raise RuntimeError(f"task {task_id} missing before live flight attach")
    stamped_summary = stamp_mission_designer_live_sitl_horizontal_summary(
        task=task,
        horizontal_summary=horizontal_summary,
    )
    attach_px4_gazebo_mission_designer_sitl_live_flight_run(
        task_id,
        horizontal_summary=stamped_summary,
        task_store_factory=lambda: gateway.task_store,
    )
    return stamped_summary


def _main() -> dict[str, Any]:
    _require_opt_in()
    horizontal_summary = _load_horizontal_summary()
    previous_runner_opt_in = os.environ.get(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV)
    sitl_upload_smoke._start_container()
    try:
        with tempfile.TemporaryDirectory(
            prefix="mission-designer-sitl-delivery-epic-exit-"
        ) as tmp:
            _configure_temp_paths(Path(tmp))

            from src.config.settings import reset_settings
            from src.gateway.server import create_gateway
            from src.runtime.task_store import reset_task_store

            reset_settings()
            reset_task_store()
            gateway = create_gateway()
            gateway.app.state.gateway = gateway
            with _docker_exec_sitl_uploader(), TestClient(gateway.app) as client:
                upload_only = _prepare_and_execute_task(
                    client,
                    owner_session_id="smoke-sitl-epic-exit-upload-only",
                )
                upload_only_task_id = upload_only["summary"]["task_id"]
                upload_only_rejected = _epic_exit_rejected(
                    client,
                    upload_only_task_id,
                    "px4_gazebo_mission_designer_sitl_flight_evidence",
                )

                missing_payload = _prepare_and_execute_task(
                    client,
                    owner_session_id="smoke-sitl-epic-exit-missing-payload",
                )
                missing_payload_task_id = missing_payload["summary"]["task_id"]
                missing_payload_summary = _attach_same_run_live_flight(
                    gateway,
                    missing_payload_task_id,
                    horizontal_summary,
                )
                attach_px4_gazebo_mission_designer_sitl_flight_evidence(
                    missing_payload_task_id,
                    horizontal_summary=missing_payload_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                missing_payload_rejected = _epic_exit_rejected(
                    client,
                    missing_payload_task_id,
                    "px4_gazebo_mission_designer_sitl_payload_release_observation",
                )

                missing_dropoff = _prepare_and_execute_task(
                    client,
                    owner_session_id="smoke-sitl-epic-exit-missing-dropoff",
                )
                missing_dropoff_task_id = missing_dropoff["summary"]["task_id"]
                missing_dropoff_summary = _attach_same_run_live_flight(
                    gateway,
                    missing_dropoff_task_id,
                    horizontal_summary,
                )
                attach_px4_gazebo_mission_designer_sitl_flight_evidence(
                    missing_dropoff_task_id,
                    horizontal_summary=missing_dropoff_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                attach_px4_gazebo_mission_designer_sitl_payload_release_observation(
                    missing_dropoff_task_id,
                    horizontal_summary=missing_dropoff_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                missing_dropoff_rejected = _epic_exit_rejected(
                    client,
                    missing_dropoff_task_id,
                    "px4_gazebo_sitl_dropoff_flight_fact",
                )

                success = _prepare_and_execute_task(
                    client,
                    owner_session_id="smoke-sitl-epic-exit-success",
                )
                task_id = success["summary"]["task_id"]
                success_summary = _attach_same_run_live_flight(
                    gateway,
                    task_id,
                    horizontal_summary,
                )
                attach_px4_gazebo_mission_designer_sitl_flight_evidence(
                    task_id,
                    horizontal_summary=success_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                attach_px4_gazebo_mission_designer_sitl_payload_release_observation(
                    task_id,
                    horizontal_summary=success_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                attach_px4_gazebo_mission_designer_sitl_dropoff_verification(
                    task_id,
                    horizontal_summary=success_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                exit_attached = (
                    attach_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result(
                        task_id,
                        prompt=PROMPT,
                        upload_only_delivery_success_rejected=upload_only_rejected,
                        missing_flight_delivery_success_rejected=upload_only_rejected,
                        missing_payload_release_delivery_success_rejected=(
                            missing_payload_rejected
                        ),
                        missing_dropoff_delivery_success_rejected=(
                            missing_dropoff_rejected
                        ),
                        task_store_factory=lambda: gateway.task_store,
                    )
                )
                stored_response = client.get(f"/tasks/{task_id}")
                stored_response.raise_for_status()
                stored = stored_response.json()["task"]

        exit_result = exit_attached[
            "px4_gazebo_mission_designer_sitl_delivery_epic_exit"
        ]
        required_artifacts = (
            "px4_gazebo_mission_scenario_proposal",
            "px4_gazebo_mission_scenario_approval",
            "px4_gazebo_mission_designer_sitl_execution_request",
            "px4_gazebo_sitl_mission_upload_receipt",
            "px4_gazebo_mission_designer_sitl_execution_result",
            "px4_gazebo_mission_designer_sitl_live_flight_run",
            "px4_gazebo_mission_designer_sitl_flight_evidence",
            "px4_gazebo_mission_designer_sitl_payload_release_observation",
            "px4_gazebo_sitl_payload_release_event",
            "px4_gazebo_sitl_dropoff_flight_fact",
            "px4_gazebo_sitl_dropoff_verification",
            "px4_gazebo_mission_designer_sitl_dropoff_verification",
            "px4_gazebo_mission_designer_sitl_delivery_epic_exit",
        )
        missing_artifacts = [
            key for key in required_artifacts if key not in stored["artifacts"]
        ]
        if missing_artifacts:
            raise RuntimeError(f"stored task missing artifacts: {missing_artifacts}")
        if exit_result["mission_designer_sitl_delivery_epic_exit_complete"] is not True:
            raise RuntimeError("epic-exit artifact did not complete")
        for key in (
            "payload_release_observed",
            "payload_release_verified",
            "dropoff_verified",
            "actual_px4_gazebo_sitl_upload_observed",
            "actual_sitl_flight_evidence_observed",
        ):
            if exit_result[key] is not True:
                raise RuntimeError(f"epic-exit artifact did not prove {key}")
        for key in (
            "hardware_target_allowed",
            "physical_execution_invoked",
            "ros_dispatch_performed",
            "actuator_execution_performed",
            "synthetic_success_allowed",
            "approval_free_stronger_execution_allowed",
        ):
            if exit_result[key] is not False:
                raise RuntimeError(f"epic-exit artifact weakened {key}")
        payload_observation = stored["artifacts"][
            "px4_gazebo_mission_designer_sitl_payload_release_observation"
        ]
        dropoff_verification = stored["artifacts"][
            "px4_gazebo_mission_designer_sitl_dropoff_verification"
        ]
        live_run = stored["artifacts"][
            "px4_gazebo_mission_designer_sitl_live_flight_run"
        ]
        live_run_ref = (
            "px4_gazebo_mission_designer_sitl_live_flight_run:"
            + live_run["live_flight_run_id"]
        )
        if payload_observation["live_flight_run_ref"] != live_run_ref:
            raise RuntimeError("payload observation is not bound to live run")
        if dropoff_verification["live_flight_run_ref"] != live_run_ref:
            raise RuntimeError("dropoff verification is not bound to live run")
        if payload_observation["mission_item_binding_sha256"] != (
            live_run["mission_item_binding_sha256"]
        ):
            raise RuntimeError("payload observation mission item binding mismatch")
        if dropoff_verification["mission_item_binding_sha256"] != (
            live_run["mission_item_binding_sha256"]
        ):
            raise RuntimeError("dropoff verification mission item binding mismatch")
        if payload_observation["payload_release_bound_to_live_run"] is not True:
            raise RuntimeError("payload observation did not prove live-run binding")
        if dropoff_verification["dropoff_verification_bound_to_live_run"] is not True:
            raise RuntimeError("dropoff verification did not prove live-run binding")

        return {
            "mission_designer_sitl_delivery_epic_exit_smoke_passed": True,
            "task_id": stored["task_id"],
            "task_status": stored["status"],
            "epic_exit_schema_version": exit_result["schema_version"],
            "mission_designer_chain_state": exit_result["mission_designer_chain_state"],
            "mission_designer_sitl_delivery_epic_exit_complete": exit_result[
                "mission_designer_sitl_delivery_epic_exit_complete"
            ],
            "actual_px4_gazebo_sitl_upload_observed": exit_result[
                "actual_px4_gazebo_sitl_upload_observed"
            ],
            "actual_sitl_flight_evidence_observed": exit_result[
                "actual_sitl_flight_evidence_observed"
            ],
            "payload_release_observed": exit_result["payload_release_observed"],
            "payload_release_event_source": exit_result["payload_release_event_source"],
            "dropoff_verified": exit_result["dropoff_verified"],
            "live_flight_run_ref": live_run_ref,
            "same_gateway_execution_run_observed": live_run[
                "same_gateway_execution_run_observed"
            ],
            "mission_item_binding_sha256": live_run["mission_item_binding_sha256"],
            "payload_release_bound_to_live_run": payload_observation[
                "payload_release_bound_to_live_run"
            ],
            "dropoff_verification_bound_to_live_run": dropoff_verification[
                "dropoff_verification_bound_to_live_run"
            ],
            "delivery_scorecard_ref": exit_result["delivery_scorecard_ref"],
            "delivery_episode_review_ref": exit_result["delivery_episode_review_ref"],
            "autonomy_gate_result_ref": exit_result["autonomy_gate_result_ref"],
            "scorecard_review_gate_evidence_source": exit_result[
                "scorecard_review_gate_evidence_source"
            ],
            "upload_only_delivery_success_rejected": upload_only_rejected,
            "missing_flight_delivery_success_rejected": upload_only_rejected,
            "missing_payload_release_delivery_success_rejected": (
                missing_payload_rejected
            ),
            "missing_dropoff_delivery_success_rejected": missing_dropoff_rejected,
            "horizontal_route_artifact_dir": horizontal_summary["artifact_dir"],
            "hardware_target_allowed": exit_result["hardware_target_allowed"],
            "physical_execution_invoked": exit_result["physical_execution_invoked"],
            "ros_dispatch_performed": exit_result["ros_dispatch_performed"],
            "actuator_execution_performed": exit_result["actuator_execution_performed"],
            "synthetic_success_allowed": exit_result["synthetic_success_allowed"],
        }
    finally:
        if previous_runner_opt_in is None:
            os.environ.pop(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV, None)
        else:
            os.environ[MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV] = (
                previous_runner_opt_in
            )
        sitl_upload_smoke._stop_container()


if __name__ == "__main__":
    smoke_summary = _main()
    print(json.dumps(smoke_summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(smoke_summary, sort_keys=True))
