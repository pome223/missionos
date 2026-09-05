"""Loopback Gateway smoke for Mission Assurance Form 2 fail-closed behavior."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")
    return path


def _runtime_evidence(invocation_id: str) -> dict[str, Any]:
    empty_sha = hashlib.sha256(b"").hexdigest()
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": "runtime_invocation_evidence.v1",
        "invocation_id": invocation_id,
        "invocation_kind": "subprocess",
        "invocation_target": "fixture",
        "invocation_started_at": now,
        "invocation_completed_at": now,
        "invocation_stdout_sha256": empty_sha,
        "invocation_stderr_sha256": empty_sha,
        "invocation_stdout_preimage": "",
        "invocation_stderr_preimage": "",
        "invocation_exit_code": 0,
        "process_pid": os.getpid(),
        "runtime_summary_path": "fixture-summary.json",
    }


def _payload_form1() -> dict[str, Any]:
    return {
        "schema_version": "drone_behavior_delta_under_payload_mass.v1",
        "audit_id": "payload_loopback_fixture",
        "generated_at": datetime.now(UTC).isoformat(),
        "causal_form": "Form 1a",
        "form1_scope": "drone_physics_or_mission_behavior",
        "condition_kind": "payload_mass_drone_behavior_delta",
        "form1_claim_supported": True,
        "payload_behavior_delta_observed": True,
        "raw_behavior_delta_above_threshold": True,
        "drone_behavior_affected": True,
        "source_binding": {
            "source_boundary_flags_safe": True,
            "source_runs_interpretable": True,
            "route_geometry_match": True,
        },
        "metrics": {
            "max_observed_delta_m": 0.8,
            "delta_threshold_m": 0.25,
            "climb_time_delta_threshold_seconds": 1.0,
            "climb_elapsed_seconds_delta_at_target_z": 1.4,
        },
        "requested": {"light_payload_kg": 0.0, "heavy_payload_kg": 1.0},
        "progress_counted": True,
        "drone_physics_affected": True,
    }


def _wind_form1() -> dict[str, Any]:
    return {
        "schema_version": "drone_behavior_delta_under_wind.v1",
        "audit_id": "wind_loopback_fixture",
        "generated_at": datetime.now(UTC).isoformat(),
        "causal_form": "Form 1a",
        "form1_scope": "drone_physics_or_mission_behavior",
        "condition_kind": "wind_drone_behavior_delta",
        "progress_counted": True,
        "drone_physics_affected": True,
        "raw_trajectory_delta_above_threshold": True,
        "observed_delta_margin_ratio": 3.0,
        "source_binding": {
            "runtime_invocation_evidence_complete": True,
            "runtime_pairing_complete": True,
            "source_boundary_flags_safe": True,
        },
        "runtime_pairing": {
            "command_argv_sha256_equal": True,
            "condition_only_env_delta": True,
        },
        "baseline_runtime_invocation_evidence": _runtime_evidence("baseline"),
        "condition_runtime_invocation_evidence": _runtime_evidence("condition"),
        "metrics": {"max_observed_delta_m": 0.75, "delta_threshold_m": 0.25},
        "requested": {"observed_wind_a_mps": 2.0, "observed_wind_b_mps": 4.0},
    }


def _feasibility(
    status: str,
    *,
    mission_situation_input_digest: str,
    sample_index: int = 100,
) -> dict[str, Any]:
    runtime_evidence = _runtime_evidence(f"feasibility-{sample_index}")
    evaluated_at = datetime.now(UTC)
    payload = {
        "schema_version": "missionos_runtime_recovery_action_feasibility.v1",
        "feasibility_status": status,
        "action": "return_to_launch",
        "candidate_parameters": {},
        "source_hazard_state_id": "hazard_loopback_fixture",
        "source_hazard_state_sha256": "b" * 64,
        "policy_ref": "policy_loopback_fixture",
        "policy_sha256": "c" * 64,
        "model_refs": {
            "battery_action_energy": "fixture_battery_model.v1",
            "temperature": None,
        },
        "telemetry_cursor": {
            "cursor_status": "complete",
            "sample_index": sample_index,
            "elapsed_seconds": float(sample_index),
        },
        "evaluated_at": evaluated_at.isoformat(),
        "freshness_deadline": (
            evaluated_at + timedelta(seconds=20)
        ).isoformat(),
        "runtime_invocation_evidence": runtime_evidence,
        "mission_situation_input_digest": mission_situation_input_digest,
        "execution_scope": "simulator",
        "blocking_reasons": ["fixture_blocked"] if status == "blocked" else [],
        "unverified_reasons": [],
        "approval_created": False,
        "dispatch_authority_created": False,
        "physical_execution_invoked": False,
        "completion_claimed": False,
        "progress_counted": False,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return {
        **payload,
        "action_feasibility_sha256": digest,
        "action_feasibility_id": f"action_feasibility_{digest[:12]}",
    }


async def _wait_for_health(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        for _ in range(100):
            with suppress(httpx.HTTPError):
                response = await client.get("/health")
                if response.status_code == 200:
                    return
            await asyncio.sleep(0.05)
    raise TimeoutError(f"Gateway did not become healthy: {base_url}")


def _configure_temp_paths(root: Path) -> None:
    os.environ["TASK_STORE_DB_PATH"] = str(root / "tasks.db")
    os.environ["MEMORY_DB_PATH"] = str(root / "memory.db")
    os.environ["AUDIT_LOG_PATH"] = str(root / "audit.log")
    os.environ["COMPUTER_TRAJECTORY_DB_PATH"] = str(root / "computer.db")
    os.environ["PHYSICAL_AI_VALIDATION_DB_PATH"] = str(root / "physical.db")


async def _main() -> dict[str, Any]:
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="missionos-assurance-form2-") as tmp:
        temp_root = Path(tmp)
        _configure_temp_paths(temp_root)
        os.chdir(temp_root)
        artifact_root = Path("output/mission_designer_behavior_delta_audits")
        command_script = temp_root / "fixture_mission_assurance_llm.py"
        command_script.write_text(
            "import json, os, sys\n"
            "sys.stdin.read()\n"
            "if os.environ.get('MISSIONOS_ASSURANCE_SMOKE_MODE') == 'invalid':\n"
            "    print('not-json')\n"
            "else:\n"
            "    print(json.dumps({\n"
            "        'proposed_response_kind': 'return',\n"
            "        'parameters': {},\n"
            "        'rationale': 'source-bound fixture judgment',\n"
            "        'expected_outcome': 'operator reviews RTL candidate',\n"
            "        'uncertainty': 'fixture only',\n"
            "        'operator_question': 'Approve or revise return?'\n"
            "    }))\n",
            encoding="utf-8",
        )
        os.environ["MISSIONOS_MISSION_ASSURANCE_COMMAND"] = (
            f"{sys.executable} {command_script}"
        )
        os.environ["MISSIONOS_ALLOW_MISSION_ASSURANCE_COMMAND_OVERRIDE"] = "1"

        from src.config.settings import reset_settings
        from src.gateway.server import create_missionos_gateway
        from src.runtime.task_store import reset_task_store
        from src.security import audit

        reset_settings()
        reset_task_store()
        audit._audit_logger = None
        gateway = create_missionos_gateway()
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
            async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                _write_json(artifact_root / "source" / "payload.json", _payload_form1())
                payload_response = await client.post(
                    "/missionos/form2a-response-selection/run", json={}
                )
                payload_body = payload_response.json()
                payload_token_count = len(
                    list(
                        artifact_root.rglob(
                            "missionos_form2a_operator_approval_token.json"
                        )
                    )
                )

                wind_source = _write_json(
                    artifact_root / "source" / "wind.json", _wind_form1()
                )
                wind_source_sha256 = hashlib.sha256(
                    wind_source.read_bytes()
                ).hexdigest()
                os.environ["MISSIONOS_ASSURANCE_SMOKE_MODE"] = "invalid"
                failure_response = await client.post(
                    "/missionos/form2a-response-selection/run", json={}
                )
                failure_body = failure_response.json()
                failure_token_count = len(
                    list(
                        artifact_root.rglob(
                            "missionos_form2a_operator_approval_token.json"
                        )
                    )
                )

                os.environ["MISSIONOS_ASSURANCE_SMOKE_MODE"] = "return"
                blocked_path = _write_json(
                    artifact_root / "feasibility" / "blocked.json",
                    _feasibility(
                        "blocked",
                        mission_situation_input_digest=wind_source_sha256,
                    ),
                )
                blocked_response = await client.post(
                    "/missionos/form2a-response-selection/run",
                    json={
                        "action_feasibility_artifact_path": str(
                            blocked_path.relative_to(artifact_root)
                        )
                    },
                )
                blocked_body = blocked_response.json()

                verified_path = _write_json(
                    artifact_root / "feasibility" / "verified.json",
                    _feasibility(
                        "verified_feasible",
                        mission_situation_input_digest=wind_source_sha256,
                    ),
                )
                verified_response = await client.post(
                    "/missionos/form2a-response-selection/run",
                    json={
                        "action_feasibility_artifact_path": str(
                            verified_path.relative_to(artifact_root)
                        )
                    },
                )
                verified_body = verified_response.json()
                current_path = _write_json(
                    artifact_root / "feasibility" / "current.json",
                    _feasibility(
                        "verified_feasible",
                        mission_situation_input_digest=wind_source_sha256,
                        sample_index=101,
                    ),
                )
                revalidation_response = await client.post(
                    "/missionos/form2a-action-revalidation/run",
                    json={
                        "current_action_feasibility_artifact_path": str(
                            current_path.relative_to(artifact_root)
                        )
                    },
                )
                revalidation_body = revalidation_response.json()
                consumption_response = await client.post(
                    "/missionos/form2a-action-consumption/run",
                    json={
                        "action_revalidation_artifact_path": (
                            revalidation_body.get("artifact_path")
                        )
                    },
                )
                consumption_body = consumption_response.json()
        finally:
            server.should_exit = True
            await server_task
            os.chdir(original_cwd)

        checks = {
            "payload_advisory": (
                payload_response.status_code == 200
                and payload_body.get("summary_status") == "form2_advisory_selected"
                and payload_body.get("response_selection", {}).get("trigger_level")
                == "level_2_inferred"
                and payload_body.get("response_selection", {}).get(
                    "selected_response_kind"
                )
                == "payload_feasibility_advisory"
                and payload_token_count == 0
            ),
            "llm_failure_escalated": (
                failure_response.status_code == 200
                and failure_body.get("summary_status") == "form2_advisory_selected"
                and failure_body.get("response_selection", {}).get(
                    "selected_response_kind"
                )
                == "operator_escalation"
                and failure_token_count == 0
            ),
            "blocked_action_escalated": (
                blocked_response.status_code == 200
                and blocked_body.get("summary_status") == "form2_advisory_selected"
                and blocked_body.get("response_selection", {}).get(
                    "action_feasibility_status"
                )
                == "blocked"
                and blocked_body.get("response_selection", {}).get("dispatch_ref")
                is None
            ),
            "verified_action_offered_only": (
                verified_response.status_code == 200
                and verified_body.get("summary_status") == "form2a_response_selected"
                and verified_body.get("response_selection", {}).get(
                    "bounded_action_kind"
                )
                == "return_to_launch"
                and verified_body.get("operator_approval_token", {}).get("status")
                == "issued_unconsumed"
                and verified_body.get("authority_boundary", {}).get(
                    "dispatch_executed_in_runtime"
                )
                is False
            ),
            "fresh_revalidation_accepted_without_authority": (
                revalidation_response.status_code == 200
                and revalidation_body.get("revalidation_status") == "valid"
                and revalidation_body.get("dispatch_authority_created") is False
                and consumption_response.status_code == 200
                and consumption_body.get("action_consumption", {}).get(
                    "action_revalidation_status"
                )
                == "valid"
                and not any(
                    str(reason).startswith("action_revalidation")
                    or str(reason).startswith(
                        "mission_assurance_dispatch_time_revalidation"
                    )
                    for reason in consumption_body.get(
                        "authority_boundary", {}
                    ).get("blocking_reasons", [])
                )
                and consumption_body.get("authority_boundary", {}).get(
                    "operator_approval_token_consumed_in_runtime"
                )
                is False
                and consumption_body.get("authority_boundary", {}).get(
                    "dispatch_executed_in_runtime"
                )
                is False
            ),
        }
        if not all(checks.values()):
            raise RuntimeError(json.dumps(checks, sort_keys=True))
        return {
            "schema_version": "missionos_mission_assurance_form2_gateway_smoke.v1",
            "gateway_loopback_smoke_passed": True,
            "transport": "loopback_http",
            "checks": checks,
            "llm_mode": "subprocess_fixture",
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
            "limitations": [
                "fixture-backed Form 1 and Action Feasibility evidence",
                "no PX4 SITL or hardware execution",
                "dispatch-time revalidation uses fresh fixture evidence",
                "no human approval, token consumption, or dispatch exercised",
            ],
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_main()), indent=2, sort_keys=True))
