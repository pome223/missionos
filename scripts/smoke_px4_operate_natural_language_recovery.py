"""Real Gateway + CLI smoke for PX4 natural-language Recovery proposals.

This smoke requires a configured hosted Recovery Agent (DeepSeek by default).
It creates only a temporary fixture task.  Natural language must create a
durable, verified proposal while approval and dispatch remain absent.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import Any

import httpx
import uvicorn

from scripts.smoke_runtime_recovery_action_feasibility_gateway import (
    _telemetry,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _configure(tmp: Path) -> None:
    os.environ["TASK_STORE_DB_PATH"] = str(tmp / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(tmp / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(
        tmp / "computer_trajectories.db"
    )
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(
        tmp / "physical_ai_validation.db"
    )
    os.environ.setdefault("MISSIONOS_LLM_BACKEND", "deepseek")
    os.environ.setdefault("MISSIONOS_DEEPSEEK_MODEL", "deepseek-v4-flash")
    os.environ.setdefault("MISSIONOS_AGENT_RUNTIME_ADK_ENABLED", "1")


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(100):
            with suppress(httpx.HTTPError):
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


def _task_artifacts(telemetry: dict[str, Any]) -> dict[str, Any]:
    position = telemetry["position"]
    return {
        "missionos_auto_mission_gui_dispatch_running_receipt": {
            "operator_recovery_request_container_path": (
                "/tmp/missionos_px4_operate_natural_language_smoke.json"
            ),
            "hardware_target_allowed": False,
            "physical_execution_invoked": False,
        },
        "missionos_runtime_recovery_agent_live_bridge": {
            "telemetry_snapshot": telemetry,
        },
        "missionos_auto_mission_runtime_snapshot": {
            "sample_index": telemetry["sample_index"],
            "elapsed_seconds": telemetry["elapsed_seconds"],
            "local_x_m": position["local_x_m"],
            "local_y_m": position["local_y_m"],
            "altitude_above_home_m": position[
                "altitude_above_home_m"
            ],
            "wind_speed_mps": telemetry["wind"]["speed_mps"],
            "heartbeat_observed": True,
            "landed": False,
        },
    }


async def _main() -> dict[str, Any]:
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        raise RuntimeError("DEEPSEEK_API_KEY is required")
    with tempfile.TemporaryDirectory(
        prefix="missionos-px4-operate-natural-language-"
    ) as tmp:
        _configure(Path(tmp))
        from src.config.settings import reset_settings
        from src.gateway.server import create_missionos_gateway
        from src.runtime.task_store import reset_task_store
        from src.security import audit

        reset_settings()
        reset_task_store()
        audit._audit_logger = None
        queued_requests: list[dict[str, Any]] = []

        def _fixture_runner_write(**kwargs: Any) -> dict[str, Any]:
            queued_requests.append(deepcopy(kwargs))
            return {
                "request_status": "queued",
                "container_name": "fixture-px4-gazebo-sitl",
                "container_path": kwargs["container_path"],
                "bytes_written": 1,
            }

        from src.gateway import server as gateway_server

        gateway_server._write_missionos_auto_operator_recovery_request_to_container = (
            _fixture_runner_write
        )
        gateway = create_missionos_gateway()
        task_id = "task_px4_operate_natural_language_smoke"
        telemetry = _telemetry(sample_index=240, battery_percent=80.0)
        gateway.task_store.create(
            task_id=task_id,
            kind="px4_gazebo_mission_designer_sitl_execution_request",
            title="PX4 operate natural-language Recovery smoke",
            status="running",
            artifacts=_task_artifacts(telemetry),
        )

        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(
            uvicorn.Config(
                gateway.app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="on",
            )
        )
        server_task = asyncio.create_task(server.serve())
        try:
            await _wait_for_health(base_url)
            command = [
                sys.executable,
                "-m",
                "missionos_cli",
                "--gateway-url",
                base_url,
                "operate",
                "--task-id",
                task_id,
            ]
            cli = await asyncio.to_thread(
                subprocess.run,
                command,
                input="大きく右へ迂回して障害物を避けて\nquit\n",
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            if cli.returncode != 0:
                raise RuntimeError(
                    "missionos operate proposal failed: "
                    + (cli.stderr or cli.stdout)
                )
            task = gateway.task_store.get(task_id) or {}
            artifacts = task.get("artifacts")
            artifacts = artifacts if isinstance(artifacts, dict) else {}
            proposal = artifacts.get(
                "missionos_runtime_recovery_last_proposal"
            )
            proposal = proposal if isinstance(proposal, dict) else {}
            result = proposal.get("runtime_recovery_agent_result")
            result = result if isinstance(result, dict) else {}
            assessment = result.get("assessment")
            assessment = assessment if isinstance(assessment, dict) else {}
            origin = proposal.get("proposal_origin")
            origin = origin if isinstance(origin, dict) else {}
            candidate = assessment.get("recovery_planner_tool_candidate")
            candidate = candidate if isinstance(candidate, dict) else {}
            from missionos_cli.chat_interaction import (
                _recovery_proposal_command,
            )

            review_command = _recovery_proposal_command(
                {
                    "selected_bounded_action": assessment.get(
                        "selected_bounded_action"
                    )
                    or candidate.get("selected_bounded_action"),
                    "proposed_parameters": assessment.get(
                        "proposed_parameters"
                    )
                    or candidate.get("proposed_parameters"),
                }
            )
            if not review_command:
                raise RuntimeError(
                    "durable proposal has no review command: "
                    + json.dumps(
                        {
                            "cli_output": cli.stdout[-2000:],
                            "artifact_keys": sorted(artifacts.keys()),
                            "proposal_keys": sorted(proposal.keys()),
                            "assessment_action": assessment.get(
                                "selected_bounded_action"
                            ),
                            "assessment_parameter_keys": sorted(
                                (
                                    assessment.get("proposed_parameters")
                                    or {}
                                ).keys()
                            ),
                            "candidate_action": candidate.get(
                                "selected_bounded_action"
                            ),
                            "candidate_parameter_keys": sorted(
                                (
                                    candidate.get("proposed_parameters")
                                    or {}
                                ).keys()
                            ),
                        },
                        sort_keys=True,
                    )
                )
            preapproval_artifacts = dict(artifacts)
            approval_cli = await asyncio.to_thread(
                subprocess.run,
                command,
                input=f"{review_command.lstrip('/')}\ny\nquit\n",
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            if approval_cli.returncode != 0:
                raise RuntimeError(
                    "missionos operate approval failed: "
                    + (approval_cli.stderr or approval_cli.stdout)
                )
            approved_task = gateway.task_store.get(task_id) or {}
            approved_artifacts = approved_task.get("artifacts")
            approved_artifacts = (
                approved_artifacts
                if isinstance(approved_artifacts, dict)
                else {}
            )
            receipt = approved_artifacts.get(
                "missionos_runtime_recovery_dispatch_receipt"
            )
            receipt = receipt if isinstance(receipt, dict) else {}
        finally:
            server.should_exit = True
            await server_task

        checks = {
            "cli_px4_label_correct": (
                "PX4/SITL telemetry" in cli.stdout
                and "current Nav2" not in cli.stdout
            ),
            "proposal_rendered": "proposal_id=" in cli.stdout,
            "proposal_durable": (
                proposal.get("proposal_status")
                == "awaiting_operator_approval"
            ),
            "hosted_deepseek_used": (
                origin.get("origin_kind") == "hosted_llm"
                and origin.get("provider")
                == "google_adk_litellm_deepseek"
            ),
            "compiler_passed": (
                assessment.get("intent_compilation", {}).get(
                    "compilation_status"
                )
                == "compiled"
            ),
            "feasibility_verified": (
                assessment.get("action_feasibility", {}).get(
                    "feasibility_status"
                )
                == "verified_feasible"
            ),
            "approval_absent": (
                "missionos_runtime_recovery_operator_approval"
                not in preapproval_artifacts
            ),
            "dispatch_absent_before_review": (
                "missionos_runtime_recovery_dispatch_receipt"
                not in preapproval_artifacts
            ),
            "separate_review_confirmed": (
                "Send AVOID OBSTACLE" in approval_cli.stdout
            ),
            "dispatch_revalidation_valid": (
                receipt.get("proposal_revalidation", {}).get(
                    "validation_status"
                )
                == "valid"
            ),
            "dispatch_authority_created_after_review": (
                receipt.get("dispatch_authority_created") is True
            ),
            "runner_request_queued": (
                receipt.get("active_runner_request_queued") is True
                and len(queued_requests) == 1
            ),
            "physical_execution_invoked": False,
            "progress_counted": False,
        }
        if not all(
            value is True
            for key, value in checks.items()
            if key
            not in {"physical_execution_invoked", "progress_counted"}
        ):
            raise RuntimeError(
                "PX4 natural-language Recovery smoke failed: "
                + json.dumps(checks, sort_keys=True)
            )
        return {
            "smoke_passed": True,
            "task_id": task_id,
            "proposal_id": proposal.get("proposal_id"),
            "selected_action": assessment.get(
                "selected_bounded_action"
            ),
            "provider": origin.get("provider"),
            "model_id": origin.get("model_id"),
            **checks,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_main()), indent=2, sort_keys=True))
