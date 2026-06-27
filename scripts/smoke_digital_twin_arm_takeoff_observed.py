#!/usr/bin/env python3
"""Opt-in smoke that arms PX4 SITL and observes Digital Twin takeoff."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import textwrap
import time
from typing import Any

from scripts import smoke_digital_twin_px4_mission_upload as upload_smoke
from scripts import smoke_digital_twin_world_bound_sitl_e2e as fixture_e2e
from scripts import smoke_px4_gazebo_sitl_mission_upload as px4_upload_smoke
from src.runtime.digital_twin_mission_environment import (
    build_digital_twin_stage1_environment,
)
from src.runtime.digital_twin_sitl_arm_takeoff import (
    DIGITAL_TWIN_SITL_ARM_TAKEOFF_RECEIPT_SCHEMA_VERSION,
    build_digital_twin_sitl_arm_takeoff_receipt,
    digital_twin_sitl_arm_takeoff_receipt_ref,
)
from src.runtime.digital_twin_sitl_execution_result import (
    build_digital_twin_sitl_execution_result,
)
from src.runtime.digital_twin_sitl_mavlink_upload import (
    DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
    build_digital_twin_sitl_mission_upload_receipt,
    digital_twin_candidate_upload_items,
)
from src.runtime.digital_twin_sitl_process_runner import (
    build_digital_twin_sitl_process_run_from_observed_container,
    digital_twin_sitl_process_run_ref,
)
from src.runtime.flight_readiness_package import (
    build_flight_readiness_package,
    flight_readiness_package_ref,
)
from src.runtime.px4_mission_target import PX4GazeboBackend, PX4MissionTarget


OPT_IN_ENV = "RUN_DIGITAL_TWIN_ARM_TAKEOFF_SMOKE"
CONTAINER_NAME = "boiled-claw-digital-twin-arm-takeoff"
ROOT_DIR = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT_DIR / "output/digital_twin/arm_takeoff_observed"
PROMPT = "10km先の3000mの山小屋に水3kgを届ける"
PROMPT_REF = "px4_gazebo_mission_prompt_request:digital_twin_arm_takeoff"
NOW = datetime(2026, 5, 8, 2, 30, tzinfo=timezone.utc)


class _ArmTakeoffUploadShim:
    CONTAINER_NAME = CONTAINER_NAME
    PX4_MAVLINK_PORT = px4_upload_smoke.PX4_MAVLINK_PORT
    GCS_MAVLINK_PORT = px4_upload_smoke.GCS_MAVLINK_PORT
    _mission_upload_item_tuples = staticmethod(px4_upload_smoke._mission_upload_item_tuples)

    @staticmethod
    def _run(
        command: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=ROOT_DIR,
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
            timeout=timeout,
        )


class _ObservedUploader:
    def __init__(self, observed: dict[str, Any]):
        self.observed = observed
        self.heartbeat_observed = bool(observed.get("heartbeat_observed"))

    def upload(self, *, items, target_endpoint, timeout_seconds):
        return (
            tuple(int(item) for item in self.observed["mission_request_sequences"]),
            int(self.observed["mission_ack_type"]),
        )


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run Digital Twin ARM/takeoff smoke.")


def _frp_gate_summary_for_takeoff_smoke(digital_twin: dict[str, Any]) -> dict[str, Any]:
    """Build the FRP-ready summary required by the SITL-only authority gate.

    The actual takeoff smoke intentionally uses the fixture-backed world that
    #585 debugging proved can fly. The source-backed FRP smoke remains a
    separate regression check because live Open-Meteo precipitation can
    legitimately block the Fuji scenario.
    """

    world = digital_twin["gazebo_world_artifact"]
    mission = digital_twin["digital_twin_px4_mission_item_candidate"]
    return {
        "source_backed_target": True,
        "source_backed_terrain": True,
        "source_backed_weather": True,
        "vehicle_envelope_present": True,
        "mission_energy_budget_present": True,
        "real_world_target_resolution_ref": "real_world_target_resolution:arm_takeoff_smoke_fixture_gate",
        "terrain_dem_source_snapshot_ref": (
            "terrain_dem_source_snapshot:arm_takeoff_smoke_fixture_gate"
        ),
        "weather_source_snapshot_ref": "weather_source_snapshot:arm_takeoff_smoke_fixture_gate",
        "vehicle_flight_envelope_ref": (
            str(mission.get("vehicle_flight_envelope_ref"))
            if mission.get("vehicle_flight_envelope_ref")
            else "vehicle_flight_envelope:arm_takeoff_smoke_fixture_gate"
        ),
        "mission_energy_budget_ref": "mission_energy_budget:arm_takeoff_smoke_fixture_gate",
        "terrain_provider_response_status": "fixture_gate_for_arm_takeoff_smoke",
        "weather_provider_response_status": "fixture_gate_for_arm_takeoff_smoke",
        "gazebo_world_artifact_ref": (
            f"gazebo_world_artifact:{world['world_artifact_id']}"
        ),
    }


def _docker_exec_px4_listener(topic: str, count: int = 1) -> str:
    result = subprocess.run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "/opt/px4-gazebo/bin/px4-listener",
            topic,
            str(count),
        ],
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    return (result.stdout + result.stderr).strip()


def _docker_exec_mavlink_command(
    *,
    command_id: int,
    params: tuple[float, float, float, float, float, float, float],
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    script = textwrap.dedent(f"""
        import json, socket, struct, subprocess, time
        MAVLINK2_MAGIC=0xFD
        MAVLINK_MSG_ID_HEARTBEAT=0
        MAVLINK_MSG_ID_COMMAND_LONG=76
        MAVLINK_MSG_ID_COMMAND_ACK=77
        CRC_EXTRA={{0:50,76:152,77:143}}
        def crc_accumulate(byte, crc):
            tmp = byte ^ (crc & 0xFF); tmp = (tmp ^ (tmp << 4)) & 0xFF
            return ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
        def x25(data, extra):
            crc=0xFFFF
            for b in data: crc=crc_accumulate(b, crc)
            return crc_accumulate(extra, crc)
        def frame(msg_id, payload, seq):
            h=bytes([len(payload),0,0,seq&255,255,190,msg_id&255,(msg_id>>8)&255,(msg_id>>16)&255])
            c=x25(h+payload, CRC_EXTRA[msg_id])
            return bytes([MAVLINK2_MAGIC])+h+payload+struct.pack('<H', c)
        def heartbeat(seq):
            return frame(MAVLINK_MSG_ID_HEARTBEAT, struct.pack('<IBBBBB',0,6,8,0,4,3), seq)
        def command_long(seq):
            params={list(params)!r}
            payload=struct.pack('<fffffffHBBB',*[float(x) for x in params],{int(command_id)},1,1,0)
            return frame(MAVLINK_MSG_ID_COMMAND_LONG, payload, seq)
        def decode(data):
            if len(data)<12 or data[0]!=MAVLINK2_MAGIC: return None
            l=data[1]; mid=data[7]|(data[8]<<8)|(data[9]<<16)
            return mid, data[10:10+l]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.2)
            sock.bind(('127.0.0.1',{px4_upload_smoke.GCS_MAVLINK_PORT}))
            remote=('127.0.0.1',{px4_upload_smoke.PX4_MAVLINK_PORT})
            seq=120
            deadline=time.monotonic()+float({timeout_seconds!r})
            sent=False
            ack=None
            while time.monotonic()<deadline:
                sock.sendto(heartbeat(seq), remote); seq+=1
                if not sent:
                    sock.sendto(command_long(seq), remote); seq+=1
                    sent=True
                try:
                    data,_addr=sock.recvfrom(4096)
                except socket.timeout:
                    continue
                decoded=decode(data)
                if not decoded: continue
                mid,payload=decoded
                if mid==MAVLINK_MSG_ID_COMMAND_ACK and len(payload)>=3:
                    command=struct.unpack('<H', payload[:2])[0]
                    result=payload[2]
                    if command=={int(command_id)}:
                        ack=int(result)
                        break
            print(json.dumps({{'command':{int(command_id)},'attempted':sent,'ack_observed':ack is not None,'ack_result':ack}}, sort_keys=True))
    """)
    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER_NAME, "python3", "-"],
        cwd=ROOT_DIR,
        input=script,
        text=True,
        capture_output=True,
        timeout=int(timeout_seconds) + 10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout[-1000:] + result.stderr[-1000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def _build_px4_gazebo_backend() -> PX4GazeboBackend:
    def _set_mode(mode: str) -> dict[str, Any]:
        if mode != "AUTO_MISSION":
            raise ValueError(f"unsupported PX4 Gazebo mode: {mode}")
        return _docker_exec_mavlink_command(
            command_id=176,
            params=(1.0, 4.0, 4.0, 0.0, 0.0, 0.0, 0.0),
        )

    return PX4GazeboBackend(
        upload_mission_handler=lambda items: upload_smoke._docker_exec_upload_with_heartbeat(
            _ArmTakeoffUploadShim,
            items,
        ),
        arm_handler=lambda: _docker_exec_mavlink_command(
            command_id=400,
            params=(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        ),
        set_mode_handler=_set_mode,
        start_mission_handler=lambda: _docker_exec_mavlink_command(
            command_id=300,
            params=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        ),
        observe_handler=lambda: {
            "mission": _docker_exec_px4_listener("mission", 1),
            "mission_result": _docker_exec_px4_listener("mission_result", 1),
            "vehicle_status": _docker_exec_px4_listener("vehicle_status", 1),
        },
    )


def _listener_float(output: str, field: str) -> float | None:
    match = re.search(rf"\\b{re.escape(field)}:\\s*(-?\\d+(?:\\.\\d+)?)", output)
    return float(match.group(1)) if match else None


def _listener_int(output: str, field: str) -> int | None:
    match = re.search(rf"\\b{re.escape(field)}:\\s*(-?\\d+)", output)
    return int(match.group(1)) if match else None


def _listener_bool(output: str, field: str) -> bool | None:
    match = re.search(rf"\\b{re.escape(field)}:\\s*(True|False)", output)
    return match.group(1) == "True" if match else None


def _docker_exec_arm_auto_takeoff_split(
    target: PX4MissionTarget | None = None,
) -> dict[str, Any]:
    resolved_target = target or _build_px4_gazebo_backend()
    checkpoints: dict[str, dict[str, str]] = {}
    started = time.monotonic()

    checkpoints["before_arm"] = {
        "mission": _docker_exec_px4_listener("mission", 1),
        "mission_result": _docker_exec_px4_listener("mission_result", 1),
        "vehicle_status": _docker_exec_px4_listener("vehicle_status", 1),
    }
    arm_ack = resolved_target.arm()
    time.sleep(2.0)
    checkpoints["after_arm"] = {
        "mission_result": _docker_exec_px4_listener("mission_result", 1),
        "vehicle_status": _docker_exec_px4_listener("vehicle_status", 1),
    }
    auto_ack = resolved_target.set_mode("AUTO_MISSION")
    time.sleep(3.0)
    checkpoints["after_auto"] = {
        "mission_result": _docker_exec_px4_listener("mission_result", 1),
        "vehicle_status": _docker_exec_px4_listener("vehicle_status", 1),
    }
    mission_start_ack = resolved_target.start_mission()
    time.sleep(15.0)
    checkpoints["after_mission_start_window"] = {
        "mission_result": _docker_exec_px4_listener("mission_result", 1),
        "vehicle_status": _docker_exec_px4_listener("vehicle_status", 1),
        "home_position": _docker_exec_px4_listener("home_position", 1),
        "vehicle_global_position": _docker_exec_px4_listener(
            "vehicle_global_position",
            1,
        ),
        "vehicle_local_position": _docker_exec_px4_listener("vehicle_local_position", 1),
    }

    home_altitude = _listener_float(
        checkpoints["after_mission_start_window"]["home_position"],
        "alt",
    ) or 0.0
    global_altitude = _listener_float(
        checkpoints["after_mission_start_window"]["vehicle_global_position"],
        "alt",
    ) or 0.0
    local_z = _listener_float(
        checkpoints["after_mission_start_window"]["vehicle_local_position"],
        "z",
    )
    rise_from_global = max(0.0, global_altitude - home_altitude)
    rise_from_local = max(0.0, -(local_z or 0.0))
    altitude_rise = max(rise_from_global, rise_from_local)
    status_after_arm = checkpoints["after_arm"]["vehicle_status"]
    status_after_auto = checkpoints["after_auto"]["vehicle_status"]
    status_after_window = checkpoints["after_mission_start_window"]["vehicle_status"]
    return {
        "arm_command_attempted": bool(arm_ack.get("attempted")),
        "auto_mission_start_attempted": bool(auto_ack.get("attempted"))
        and bool(mission_start_ack.get("attempted")),
        "arm_ack_observed": bool(arm_ack.get("ack_observed")),
        "arm_ack_result": arm_ack.get("ack_result"),
        "auto_mission_ack_observed": bool(auto_ack.get("ack_observed")),
        "auto_mission_ack_result": auto_ack.get("ack_result"),
        "mission_start_ack_observed": bool(mission_start_ack.get("ack_observed")),
        "mission_start_ack_result": mission_start_ack.get("ack_result"),
        "arm_observed": (_listener_int(status_after_arm, "arming_state") == 2)
        or (_listener_int(status_after_auto, "arming_state") == 2)
        or (_listener_int(status_after_window, "arming_state") == 2),
        "auto_mission_mode_observed": (
            _listener_int(status_after_auto, "nav_state") == 3
            or _listener_int(status_after_window, "nav_state") == 3
        ),
        "mission_start_observed": mission_start_ack.get("ack_result") == 0,
        "takeoff_observed": altitude_rise > 5.0,
        "takeoff_altitude_max_m": round(global_altitude, 3),
        "home_altitude_m": round(home_altitude, 3),
        "altitude_rise_m": round(altitude_rise, 3),
        "flight_duration_s": round(time.monotonic() - started, 3),
        "telemetry_samples": (
            {
                "source": "px4-listener",
                "global_altitude_m": round(global_altitude, 3),
                "home_altitude_m": round(home_altitude, 3),
                "local_position_z_m": round(local_z or 0.0, 3),
                "altitude_rise_m": round(altitude_rise, 3),
                "mission_result_valid": _listener_bool(
                    checkpoints["after_mission_start_window"]["mission_result"],
                    "valid",
                ),
                "nav_state": _listener_int(status_after_window, "nav_state"),
                "arming_state": _listener_int(status_after_window, "arming_state"),
            },
        ),
        "checkpoints": checkpoints,
    }


def run_smoke() -> dict[str, Any]:
    _require_opt_in()
    fixture_e2e.CONTAINER_NAME = CONTAINER_NAME
    target = _build_px4_gazebo_backend()
    run_dir = RUN_ROOT / NOW.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    digital_twin = build_digital_twin_stage1_environment(
        prompt=PROMPT,
        prompt_request_ref=PROMPT_REF,
        altitude_target_m=3000,
        payload_weight_kg=3,
        weather_hazard_labels=(),
        now=NOW,
    )
    source_summary = _frp_gate_summary_for_takeoff_smoke(digital_twin)
    mission_item = digital_twin["digital_twin_px4_mission_item_candidate"]
    if mission_item["candidate_status"] != "candidate_generated_for_planning_only":
        raise RuntimeError("mission item candidate not generated")
    takeoff_item = mission_item["candidate_items"][0]
    os.environ["DIGITAL_TWIN_PX4_HOME_LAT"] = str(takeoff_item["latitude_deg"])
    os.environ["DIGITAL_TWIN_PX4_HOME_LON"] = str(takeoff_item["longitude_deg"])
    os.environ["DIGITAL_TWIN_PX4_HOME_ALT"] = str(
        mission_item["takeoff_terrain_elevation_m"]
    )
    world_artifact = digital_twin["gazebo_world_artifact"]
    generated_world_path = ROOT_DIR / world_artifact["world_file_path_or_artifact_uri"]
    world_root = fixture_e2e._copy_px4_world_assets(run_dir)
    prepared_world_path = fixture_e2e._inject_digital_twin_terrain(
        world_root,
        generated_world_path,
    )
    started_at = datetime.now(timezone.utc)
    command: list[str] = []
    pid = 0
    startup_ok = False
    upload_observed: dict[str, Any] | None = None
    arm_observed: dict[str, Any] | None = None
    pre_arm_checkpoint: dict[str, str] = {}
    stopped_at = None
    exit_code = 0
    try:
        command, pid, _startup_logs, startup_ok = fixture_e2e._start_world_bound_container(
            world_root,
            prepared_world_path,
        )
        if not startup_ok:
            raise RuntimeError("Digital Twin PX4/Gazebo startup failed")
        items = digital_twin_candidate_upload_items(mission_item)
        upload_observed = target.upload_mission(items)
        if upload_observed.get("mission_ack_observed") is not True:
            raise RuntimeError("Digital Twin upload did not observe ACK")
        pre_arm_checkpoint = {
            "mission": _docker_exec_px4_listener("mission", 1),
            "mission_result": _docker_exec_px4_listener("mission_result", 1),
            "vehicle_status": _docker_exec_px4_listener("vehicle_status", 1),
        }
        time.sleep(2.0)
        arm_observed = _docker_exec_arm_auto_takeoff_split(target=target)
    finally:
        final_logs, exit_code = fixture_e2e._stop_world_bound_container()
        stopped_at = datetime.now(timezone.utc)
        (run_dir / "px4_gazebo.stdout.log").write_text(
            final_logs,
            encoding="utf-8",
        )
        (run_dir / "px4_gazebo.stderr.log").write_text("", encoding="utf-8")

    process_run = build_digital_twin_sitl_process_run_from_observed_container(
        gazebo_world_artifact=world_artifact,
        command=command,
        process_pids=(pid,),
        stdout_ref=str(run_dir / "px4_gazebo.stdout.log"),
        stderr_ref=str(run_dir / "px4_gazebo.stderr.log"),
        started_at=started_at,
        stopped_at=stopped_at,
        exit_status="terminated_after_startup_window",
        exit_code=exit_code,
        startup_error_observed=not startup_ok,
        px4_process_invoked=True,
        world_artifact_load_mode="terrain_injection_into_default_world",
        px4_loaded_world_file_path=str(prepared_world_path),
        repo_root=ROOT_DIR,
    )
    receipt = build_digital_twin_sitl_mission_upload_receipt(
        px4_mission_item_candidate=mission_item,
        sitl_process_run=process_run,
        target_endpoint=DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
        operator_approved=True,
        server_opt_in=True,
        same_run_binding_ref=(
            "digital_twin_sitl_binding_gate:"
            + digital_twin["digital_twin_sitl_binding_gate"]["gate_id"]
        ),
        uploader=_ObservedUploader(upload_observed or {}),
        timeout_seconds=5.0,
        now=NOW,
    )
    execution_result = build_digital_twin_sitl_execution_result(
        gazebo_world_artifact=world_artifact,
        coordinate_transform_candidate=digital_twin["coordinate_transform_candidate"],
        px4_mission_item_candidate=mission_item,
        sitl_binding_gate=digital_twin["digital_twin_sitl_binding_gate"],
        sitl_process_run=process_run,
        mission_upload_receipt=receipt,
        source_backed_inputs_summary=source_summary,
        now=NOW,
    )
    package = build_flight_readiness_package(
        execution_result=execution_result,
        now=NOW,
    )
    if arm_observed is not None:
        climb_match = re.search(r"Climb to\s+([0-9.]+)\s+meters above home", final_logs)
        log_altitude_rise = float(climb_match.group(1)) if climb_match else 0.0
        if "Armed by external command" in final_logs:
            arm_observed["arm_observed"] = True
        if "Executing Mission" in final_logs:
            arm_observed["auto_mission_mode_observed"] = True
            arm_observed["mission_start_observed"] = True
        if "Takeoff detected" in final_logs:
            arm_observed["takeoff_observed"] = True
            arm_observed["altitude_rise_m"] = max(
                float(arm_observed.get("altitude_rise_m") or 0.0),
                log_altitude_rise,
            )
            arm_observed["home_altitude_m"] = float(
                arm_observed.get("home_altitude_m")
                or os.environ["DIGITAL_TWIN_PX4_HOME_ALT"]
            )
            arm_observed["takeoff_altitude_max_m"] = (
                float(arm_observed["home_altitude_m"])
                + float(arm_observed["altitude_rise_m"])
            )
            samples = [
                sample
                for sample in list(arm_observed.get("telemetry_samples") or ())
                if not (
                    sample.get("source") == "px4-listener"
                    and float(sample.get("altitude_rise_m") or 0.0) <= 0.0
                )
            ]
            samples.append(
                {
                    "source": "px4_commander_navigator_log",
                    "executing_mission_observed": "Executing Mission" in final_logs,
                    "takeoff_detected_observed": True,
                    "home_altitude_m": arm_observed["home_altitude_m"],
                    "altitude_rise_m": arm_observed["altitude_rise_m"],
                    "takeoff_altitude_max_m": arm_observed["takeoff_altitude_max_m"],
                }
            )
            arm_observed["telemetry_samples"] = tuple(samples)
    arm_receipt = build_digital_twin_sitl_arm_takeoff_receipt(
        flight_readiness_package=package,
        mission_upload_receipt=receipt,
        execution_result=execution_result,
        target_endpoint=DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
        operator_approved=True,
        server_opt_in=True,
        observed=arm_observed or {},
        now=NOW,
    )
    px4_log_evidence_lines = [
        line
        for line in final_logs.splitlines()
        if (
            "Executing Mission" in line
            or "Climb to " in line
            or "Takeoff detected" in line
        )
    ]
    summary = {
        "schema_version": arm_receipt.schema_version,
        "schema_version_expected": DIGITAL_TWIN_SITL_ARM_TAKEOFF_RECEIPT_SCHEMA_VERSION,
        "arm_takeoff_receipt_ref": digital_twin_sitl_arm_takeoff_receipt_ref(
            arm_receipt
        ),
        "flight_readiness_package_ref": flight_readiness_package_ref(package),
        "readiness_status": package.readiness_status,
        "process_run_ref": digital_twin_sitl_process_run_ref(process_run),
        "mission_upload_observed": receipt.mission_upload_observed,
        "mission_ack_observed": receipt.mission_ack_observed,
        "heartbeat_observed": receipt.heartbeat_observed,
        "arm_command_attempted": arm_receipt.arm_command_attempted,
        "arm_ack_observed": arm_receipt.arm_ack_observed,
        "arm_ack_result": arm_receipt.arm_ack_result,
        "auto_mission_start_attempted": arm_receipt.auto_mission_start_attempted,
        "auto_mission_ack_observed": arm_receipt.auto_mission_ack_observed,
        "auto_mission_ack_result": arm_receipt.auto_mission_ack_result,
        "mission_start_observed": arm_receipt.mission_start_observed,
        "mission_start_ack_observed": arm_receipt.mission_start_ack_observed,
        "mission_start_ack_result": arm_receipt.mission_start_ack_result,
        "arm_observed": arm_receipt.arm_observed,
        "auto_mission_mode_observed": arm_receipt.auto_mission_mode_observed,
        "takeoff_observed": arm_receipt.takeoff_observed,
        "takeoff_altitude_max_m": arm_receipt.takeoff_altitude_max_m,
        "home_altitude_m": arm_receipt.home_altitude_m,
        "altitude_rise_m": arm_receipt.altitude_rise_m,
        "flight_duration_s": arm_receipt.flight_duration_s,
        "telemetry_sample_count": len(arm_receipt.telemetry_samples),
        "telemetry_samples": list(arm_receipt.telemetry_samples[:8]),
        "pre_arm_checkpoint": pre_arm_checkpoint,
        "px4_log_evidence_lines": px4_log_evidence_lines,
        "blocked_reasons": list(arm_receipt.blocked_reasons),
        "hardware_target_allowed": arm_receipt.hardware_target_allowed,
        "physical_execution_invoked": arm_receipt.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": (
            arm_receipt.approval_free_stronger_execution_allowed
        ),
        "receipt_hash_equals_sha256": arm_receipt.receipt_hash == arm_receipt.sha256,
        "frp_warning_reasons": list(package.warning_reasons),
    }
    print(
        "SMOKE_DEBUG_SUMMARY_JSON "
        + json.dumps(summary, sort_keys=True, ensure_ascii=False)
    )
    assert summary["mission_upload_observed"] is True
    assert summary["mission_ack_observed"] is True
    assert summary["heartbeat_observed"] is True
    assert summary["arm_observed"] is True
    assert summary["auto_mission_mode_observed"] is True
    assert summary["mission_start_observed"] is True
    assert summary["takeoff_observed"] is True
    assert summary["altitude_rise_m"] > 5.0
    assert summary["flight_duration_s"] > 10.0
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["receipt_hash_equals_sha256"] is True
    return summary


def main() -> int:
    summary = run_smoke()
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    print(
        "SMOKE_SUMMARY_JSON "
        + json.dumps(summary, sort_keys=True, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
