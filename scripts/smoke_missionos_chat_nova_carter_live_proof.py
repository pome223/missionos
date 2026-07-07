#!/usr/bin/env python3
"""Opt-in MissionOS chat -> Nova Carter live-proof smoke.

Default mode is intentionally dry: it prints the env and artifact checklist
without starting the Gateway or sending a Nav2 goal. Set
RUN_MISSIONOS_CHAT_NOVA_CARTER_LIVE_PROOF=1 on an RTX Isaac Sim host to run the
source-bound plan -> approve -> execute flow and require live simulator evidence.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
from urllib import error, request

from src.runtime.ros2_nav2_dispatch_bridge import (
    ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV,
    ROS2_NAV2_BRIDGE_COMMAND_ENV,
)

LIVE_PROOF_ENV = "RUN_MISSIONOS_CHAT_NOVA_CARTER_LIVE_PROOF"
INSTRUCTION_ENV = "MISSIONOS_CHAT_NOVA_CARTER_LIVE_PROOF_INSTRUCTION"
SESSION_ID_ENV = "MISSIONOS_CHAT_NOVA_CARTER_LIVE_PROOF_SESSION_ID"
HTTP_TIMEOUT_ENV = "MISSIONOS_CHAT_NOVA_CARTER_HTTP_TIMEOUT_SECONDS"
ARTIFACT_MANIFEST_OUT_ENV = "MISSIONOS_CHAT_NOVA_CARTER_ARTIFACT_MANIFEST_OUT"
WATCH_ARTIFACT_ENV = "MISSIONOS_CHAT_NOVA_CARTER_WATCH_ARTIFACT"
MAP_ARTIFACT_ENV = "MISSIONOS_CHAT_NOVA_CARTER_MAP_ARTIFACT"
DEFAULT_INSTRUCTION = "Nova CarterでIsaac Sim内の短いNav2ルートを走って"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _http_timeout_seconds() -> float:
    raw_value = os.environ.get(HTTP_TIMEOUT_ENV)
    try:
        timeout = float(raw_value) if raw_value is not None else 600.0
    except ValueError:
        return 600.0
    return timeout if timeout > 0 else 600.0


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gateway_env() -> dict[str, str]:
    env = os.environ.copy()
    env["MISSIONOS_GATEWAY_BACKEND"] = "production"
    return env


def _start_gateway() -> tuple[str, subprocess.Popen[bytes]]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "missionos_gateway",
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_gateway_env(),
    )
    _wait_for_gateway(base_url, proc)
    return base_url, proc


def _stop_gateway(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _wait_for_gateway(base_url: str, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            gateway_output = ""
            if proc.stdout is not None:
                gateway_output = proc.stdout.read().decode(
                    "utf-8",
                    errors="replace",
                )
            raise RuntimeError(
                "Gateway exited before health was reachable"
                f"\n{gateway_output[-4000:]}"
            )
        try:
            with request.urlopen(f"{base_url}/health", timeout=1.0) as response:
                if response.status == 200:
                    return
        except (OSError, error.URLError):
            pass
        time.sleep(0.25)
    raise RuntimeError("Timed out waiting for Gateway health")


def _post_json(url: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(http_request, timeout=timeout_s) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Gateway returned HTTP {response.status}")
        data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Gateway returned non-object JSON")
        return data


def _post_conversation(
    *,
    base_url: str,
    instruction: str,
    session_id: str,
    route_hint: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operator_instruction": instruction,
        "robot_profile": "nova_carter",
        "missionos_client_surface": "chat",
        "missionos_route_hint": route_hint,
        "session_id": session_id,
    }
    if context:
        payload["mission_designer_context"] = context
    return _post_json(
        f"{base_url}/missionos/autonomy-conversation/run",
        payload,
        timeout_s=_http_timeout_seconds(),
    )


def _mission_designer(payload: dict[str, Any]) -> dict[str, Any]:
    mission_designer = payload.get("mission_designer")
    return dict(mission_designer) if isinstance(mission_designer, dict) else {}


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = _mission_designer(payload).get("summary")
    return dict(summary) if isinstance(summary, dict) else {}


def _execution(payload: dict[str, Any]) -> dict[str, Any]:
    execution = _mission_designer(payload).get("turtlebot3_home_mission_execution")
    return dict(execution) if isinstance(execution, dict) else {}


def _task_id(payload: dict[str, Any]) -> str:
    mission_designer = _mission_designer(payload)
    summary = _summary(payload)
    task = mission_designer.get("turtlebot3_home_mission_task")
    task = task if isinstance(task, dict) else {}
    value = (
        summary.get("task_id")
        or summary.get("turtlebot3_home_mission_task_id")
        or task.get("task_id")
    )
    return str(value or "")


def _adapter_evidence(execution: dict[str, Any]) -> dict[str, Any]:
    evidence = execution.get("adapter_evidence")
    return dict(evidence) if isinstance(evidence, dict) else {}


def _ack_observed(execution: dict[str, Any]) -> bool:
    if execution.get("nav2_goal_acknowledged") is True:
        return True
    evidence = _adapter_evidence(execution)
    if evidence.get("command_ack_observed") is True:
        return str(evidence.get("ack_status") or "").lower() == "accepted"
    for response in execution.get("bridge_responses") or []:
        if not isinstance(response, dict):
            continue
        status = str(response.get("ack_status") or response.get("status") or "").lower()
        if response.get("accepted") is True or status in {"accepted", "active", "succeeded"}:
            return True
    return False


def _odom_delta_m(execution: dict[str, Any], summary: dict[str, Any]) -> float | None:
    value = execution.get("odom_delta_m")
    if value is None:
        value = summary.get("odom_delta_m")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_live_proof_manifest(
    *,
    plan: dict[str, Any],
    approved: dict[str, Any],
    executed: dict[str, Any],
) -> dict[str, Any]:
    """Build the external-proof checklist without strengthening claims."""

    run_summary = _summary(executed)
    execution = _execution(executed)
    odom_delta_m = _odom_delta_m(execution, run_summary)
    ack_observed = _ack_observed(execution)
    task_id = _task_id(executed)
    map_artifact = os.environ.get(MAP_ARTIFACT_ENV, "").strip()
    watch_artifact = os.environ.get(WATCH_ARTIFACT_ENV, "").strip()
    required_runtime_evidence = {
        "robot_profile": run_summary.get("robot_profile") == "nova_carter",
        "execution_target": (
            run_summary.get("execution_target")
            == "isaac_ros_nav2_nova_carter_sim"
        ),
        "task_id_present": bool(task_id),
        "dispatch_request_sent": execution.get("dispatch_request_sent") is True,
        "ack_observed": ack_observed,
        "robot_motion_observed": execution.get("robot_motion_observed") is True,
        "odom_delta_present": odom_delta_m is not None and odom_delta_m > 0.0,
        "completion_claimed": run_summary.get("completion_claimed") is True,
        "completion_scope": run_summary.get("completion_scope") == "sim_action",
        "physical_execution_not_invoked": (
            run_summary.get("physical_execution_invoked") is False
        ),
        "mission_delivery_not_claimed": (
            run_summary.get("mission_delivery_completion_claimed") is False
        ),
    }
    required_external_proof = {
        **required_runtime_evidence,
        "map_artifact_recorded": bool(map_artifact),
        "watch_artifact_recorded": bool(watch_artifact),
    }
    runtime_evidence_ready = all(required_runtime_evidence.values())
    ready_for_external_claim = all(required_external_proof.values())
    return {
        "schema_version": "missionos_nova_carter_live_proof_manifest.v1",
        "smoke": "missionos_chat_nova_carter_live_proof",
        "runtime_evidence_ready": runtime_evidence_ready,
        "ready_for_external_claim": ready_for_external_claim,
        "plan_route": plan.get("routed_action"),
        "approve_route": approved.get("routed_action"),
        "execute_route": executed.get("routed_action"),
        "task_id": task_id,
        "robot_profile": run_summary.get("robot_profile"),
        "execution_target": run_summary.get("execution_target"),
        "runtime_substrate": run_summary.get("runtime_substrate"),
        "runtime_configuration_status": run_summary.get(
            "runtime_configuration_status"
        ),
        "status": run_summary.get("status"),
        "dispatch_request_sent": execution.get("dispatch_request_sent"),
        "ack_observed": ack_observed,
        "robot_motion_observed": execution.get("robot_motion_observed"),
        "odom_delta_m": odom_delta_m,
        "completion_claimed": run_summary.get("completion_claimed"),
        "completion_scope": run_summary.get("completion_scope"),
        "physical_execution_invoked": run_summary.get("physical_execution_invoked"),
        "mission_delivery_completion_claimed": run_summary.get(
            "mission_delivery_completion_claimed"
        ),
        "blocking_reasons": run_summary.get("blocking_reasons") or [],
        "map_artifact": map_artifact,
        "watch_artifact": watch_artifact,
        "required_runtime_evidence": required_runtime_evidence,
        "required_external_proof": required_external_proof,
        "required_proof": required_external_proof,
        "claim_boundary": (
            "This manifest can support NVIDIA Isaac Sim / Nova Carter runtime "
            "proof only when ready_for_external_claim=true. It never supports "
            "physical execution or mission delivery completion claims."
        ),
    }


def _dry_run_manifest() -> dict[str, Any]:
    return {
        "schema_version": "missionos_nova_carter_live_proof_manifest.v1",
        "smoke": "missionos_chat_nova_carter_live_proof",
        "ran": False,
        "reason": f"{LIVE_PROOF_ENV} is not set to 1; no Gateway or Nav2 goal was sent.",
        "required_env": {
            LIVE_PROOF_ENV: "1",
            ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV: "1",
            ROS2_NAV2_BRIDGE_COMMAND_ENV: "<operator-provided Nova Carter Nav2 bridge command>",
        },
        "optional_artifact_env": {
            MAP_ARTIFACT_ENV: "path or URI from missionos map --snapshot",
            WATCH_ARTIFACT_ENV: "path or URI from missionos watch capture",
            ARTIFACT_MANIFEST_OUT_ENV: "path for this manifest JSON",
        },
        "dispatch_request_sent": False,
        "completion_claimed": False,
        "completion_scope": "none",
        "physical_execution_invoked": False,
        "mission_delivery_completion_claimed": False,
    }


def _write_manifest(manifest: dict[str, Any]) -> None:
    output = os.environ.get(ARTIFACT_MANIFEST_OUT_ENV, "").strip()
    if not output:
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    if not _truthy_env(LIVE_PROOF_ENV):
        manifest = _dry_run_manifest()
        print(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True))
        _write_manifest(manifest)
        return 0

    if not _truthy_env(ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV):
        raise SystemExit(f"{ROS2_NAV2_BOUNDED_DISPATCH_SMOKE_ENV}=1 is required")
    if not os.environ.get(ROS2_NAV2_BRIDGE_COMMAND_ENV, "").strip():
        raise SystemExit(f"{ROS2_NAV2_BRIDGE_COMMAND_ENV} is required")

    base_url, proc = _start_gateway()
    try:
        session_id = os.environ.get(
            SESSION_ID_ENV,
            "smoke-chat-nova-carter-live-proof",
        )
        instruction = os.environ.get(INSTRUCTION_ENV, DEFAULT_INSTRUCTION)
        plan = _post_conversation(
            base_url=base_url,
            instruction=instruction,
            session_id=session_id,
            route_hint="mission_designer_plan",
        )
        approved = _post_conversation(
            base_url=base_url,
            instruction="Approve the current MissionOS plan.",
            session_id=session_id,
            route_hint="approve",
            context=_mission_designer(plan),
        )
        executed = _post_conversation(
            base_url=base_url,
            instruction="Run the current bounded action through the MissionOS execution gate.",
            session_id=session_id,
            route_hint="execute",
            context=_mission_designer(approved),
        )
    finally:
        _stop_gateway(proc)

    manifest = build_live_proof_manifest(
        plan=plan,
        approved=approved,
        executed=executed,
    )
    print(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True))
    _write_manifest(manifest)
    return 0 if manifest["runtime_evidence_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
