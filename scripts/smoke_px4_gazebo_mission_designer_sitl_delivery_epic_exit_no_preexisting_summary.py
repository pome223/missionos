"""No-preexisting-summary epic-exit smoke for Mission Designer SITL delivery.

This smoke exercises the Gateway live SITL execution route end to end. Unlike
the compatibility smoke, it does not accept a horizontal-route summary path as
input. The only horizontal summary it consumes is the one produced by the
Gateway-triggered live flight runner in this process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from fastapi.testclient import TestClient

from scripts import smoke_px4_gazebo_sitl_mission_upload as sitl_upload_smoke
from scripts.smoke_px4_gazebo_mission_designer_sitl_delivery_epic_exit import (
    PROMPT,
    SUMMARY_PATH_ENV,
    _configure_temp_paths,
    _docker_exec_sitl_uploader,
    _epic_exit_rejected,
    _post_json,
)
from src.runtime.px4_gazebo_mission_designer_sitl_delivery_epic_exit import (
    attach_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result,
)
from src.runtime.px4_gazebo_mission_designer_sitl_live_flight_run import (
    MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV,
)
from src.runtime.px4_gazebo_mission_designer_sitl_runner import (
    MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV,
    attach_px4_gazebo_mission_designer_sitl_dropoff_verification,
    attach_px4_gazebo_mission_designer_sitl_flight_evidence,
    attach_px4_gazebo_mission_designer_sitl_payload_release_observation,
)

OPT_IN_ENV = "RUN_MISSION_DESIGNER_SITL_DELIVERY_NO_PREEXISTING_EPIC_EXIT_SMOKE"


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run this smoke.")
    if os.getenv(SUMMARY_PATH_ENV):
        raise SystemExit(
            f"{SUMMARY_PATH_ENV} is forbidden for the no-preexisting-summary smoke."
        )


def _configure_live_temp_paths(tmp: Path) -> None:
    _configure_temp_paths(tmp)
    os.environ[MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV] = "1"
    os.environ[MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV] = "1"


def _prepare_and_execute_live_task(
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
            "live_flight_mode": True,
        },
    )
    summary = executed["summary"]
    if summary["upload_status"] != "uploaded":
        raise RuntimeError("Gateway execute route did not observe upload")
    if summary["live_flight_status"] != "completed":
        raise RuntimeError("Gateway live flight runner did not complete")
    if summary["live_flight_runner_invoked"] is not True:
        raise RuntimeError("Gateway live flight runner was not invoked")
    if summary["preexisting_summary_input_allowed"] is not False:
        raise RuntimeError("live flight run allowed preexisting summary input")
    return executed


def _prepare_and_execute_upload_only_task(
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
    summary = executed["summary"]
    if summary["upload_status"] != "uploaded":
        raise RuntimeError("Gateway upload-only route did not observe upload")
    if executed["live_flight_mode_requested"] is not False:
        raise RuntimeError("upload-only negative unexpectedly requested live mode")
    if "px4_gazebo_mission_designer_sitl_live_flight_run" in (
        executed["task"].get("artifacts") or {}
    ):
        raise RuntimeError("upload-only negative unexpectedly created live run")
    return executed


def _load_live_summary_from_task(task: dict[str, Any]) -> dict[str, Any]:
    live_run = task["artifacts"]["px4_gazebo_mission_designer_sitl_live_flight_run"]
    summary_path = Path(live_run["horizontal_summary_artifact_dir"]) / "summary.json"
    if not summary_path.is_file():
        raise RuntimeError("Gateway live flight run did not persist summary.json")
    summary = json.loads(summary_path.read_text())
    required_true = (
        "actual_px4_gazebo_horizontal_smoke_observed",
        "same_gateway_execution_run_observed",
        "preupload_mission_performed",
        "preupload_mission_ack_observed",
        "dropoff_region_reached",
        "payload_release_observed",
    )
    for key in required_true:
        if summary.get(key) is not True:
            raise RuntimeError(f"live epic-exit smoke requires {key}=true")
    if summary.get("payload_release_event_source") != (
        "gazebo_detachable_joint_detach_event"
    ):
        raise RuntimeError("live smoke requires detachable-joint payload release")
    if summary.get("mission_designer_task_id") != task["task_id"]:
        raise RuntimeError("live summary task binding mismatch")
    return summary


def _main() -> dict[str, Any]:
    _require_opt_in()
    previous_sitl_opt_in = os.environ.get(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV)
    previous_live_opt_in = os.environ.get(MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV)
    sitl_upload_smoke._start_container()
    try:
        with tempfile.TemporaryDirectory(
            prefix="mission-designer-sitl-no-preexisting-epic-exit-"
        ) as tmp:
            _configure_live_temp_paths(Path(tmp))

            from src.config.settings import reset_settings
            from src.gateway.server import create_gateway
            from src.runtime.task_store import reset_task_store

            reset_settings()
            reset_task_store()
            gateway = create_gateway()
            gateway.app.state.gateway = gateway
            with _docker_exec_sitl_uploader(), TestClient(gateway.app) as client:
                upload_only = _prepare_and_execute_upload_only_task(
                    client,
                    owner_session_id="smoke-sitl-no-preexisting-upload-only",
                )
                upload_only_task_id = upload_only["summary"]["task_id"]
                upload_only_rejected = _epic_exit_rejected(
                    client,
                    upload_only_task_id,
                    "px4_gazebo_mission_designer_sitl_flight_evidence",
                )

                executed = _prepare_and_execute_live_task(
                    client,
                    owner_session_id="smoke-sitl-no-preexisting-epic-exit",
                )
                task_id = executed["summary"]["task_id"]
                missing_flight_rejected = _epic_exit_rejected(
                    client,
                    task_id,
                    "px4_gazebo_mission_designer_sitl_flight_evidence",
                )

                task = gateway.task_store.get(task_id)
                if task is None:
                    raise RuntimeError("live task disappeared before evidence attach")
                horizontal_summary = _load_live_summary_from_task(task)
                attach_px4_gazebo_mission_designer_sitl_flight_evidence(
                    task_id,
                    horizontal_summary=horizontal_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                missing_payload_rejected = _epic_exit_rejected(
                    client,
                    task_id,
                    "px4_gazebo_mission_designer_sitl_payload_release_observation",
                )

                attach_px4_gazebo_mission_designer_sitl_payload_release_observation(
                    task_id,
                    horizontal_summary=horizontal_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                missing_dropoff_rejected = _epic_exit_rejected(
                    client,
                    task_id,
                    "px4_gazebo_sitl_dropoff_flight_fact",
                )

                attach_px4_gazebo_mission_designer_sitl_dropoff_verification(
                    task_id,
                    horizontal_summary=horizontal_summary,
                    task_store_factory=lambda: gateway.task_store,
                )
                exit_attached = (
                    attach_px4_gazebo_mission_designer_sitl_delivery_epic_exit_result(
                        task_id,
                        prompt=PROMPT,
                        upload_only_delivery_success_rejected=upload_only_rejected,
                        missing_flight_delivery_success_rejected=(
                            missing_flight_rejected
                        ),
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
        live_run = stored["artifacts"][
            "px4_gazebo_mission_designer_sitl_live_flight_run"
        ]
        payload_observation = stored["artifacts"][
            "px4_gazebo_mission_designer_sitl_payload_release_observation"
        ]
        dropoff_verification = stored["artifacts"][
            "px4_gazebo_mission_designer_sitl_dropoff_verification"
        ]
        live_run_ref = (
            "px4_gazebo_mission_designer_sitl_live_flight_run:"
            + live_run["live_flight_run_id"]
        )
        if exit_result["mission_designer_sitl_delivery_epic_exit_complete"] is not True:
            raise RuntimeError("no-preexisting epic-exit artifact did not complete")
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
        for key in (
            "hardware_target_allowed",
            "physical_execution_invoked",
            "ros_dispatch_performed",
            "actuator_execution_performed",
            "synthetic_success_allowed",
        ):
            if exit_result[key] is not False:
                raise RuntimeError(f"epic-exit artifact weakened {key}")

        return {
            "mission_designer_sitl_delivery_no_preexisting_epic_exit_smoke_passed": (
                True
            ),
            "preexisting_summary_input_used": False,
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
            "actual_px4_gazebo_horizontal_smoke_observed": live_run[
                "actual_px4_gazebo_horizontal_smoke_observed"
            ],
            "live_flight_runner_invoked": True,
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
            "upload_only_delivery_success_rejected": upload_only_rejected,
            "missing_flight_delivery_success_rejected": missing_flight_rejected,
            "missing_payload_release_delivery_success_rejected": (
                missing_payload_rejected
            ),
            "missing_dropoff_delivery_success_rejected": missing_dropoff_rejected,
            "horizontal_route_artifact_dir": live_run[
                "horizontal_summary_artifact_dir"
            ],
            "hardware_target_allowed": exit_result["hardware_target_allowed"],
            "physical_execution_invoked": exit_result["physical_execution_invoked"],
            "ros_dispatch_performed": exit_result["ros_dispatch_performed"],
            "actuator_execution_performed": exit_result["actuator_execution_performed"],
            "synthetic_success_allowed": exit_result["synthetic_success_allowed"],
        }
    finally:
        if previous_sitl_opt_in is None:
            os.environ.pop(MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV, None)
        else:
            os.environ[MISSION_DESIGNER_SITL_EXECUTION_OPT_IN_ENV] = (
                previous_sitl_opt_in
            )
        if previous_live_opt_in is None:
            os.environ.pop(MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV, None)
        else:
            os.environ[MISSION_DESIGNER_LIVE_SITL_FLIGHT_OPT_IN_ENV] = (
                previous_live_opt_in
            )
        sitl_upload_smoke._stop_container()


if __name__ == "__main__":
    smoke_summary = _main()
    print(json.dumps(smoke_summary, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(smoke_summary, sort_keys=True))
