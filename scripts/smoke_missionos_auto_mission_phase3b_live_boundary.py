#!/usr/bin/env python3
"""Opt-in Phase 3B live boundary for the MissionOS AUTO mission runner.

The smoke proves only this boundary against real PX4/Gazebo SITL:
mission upload accepted -> ARM ACK -> AUTO.MISSION ACK -> immediate LAND ACK.
It intentionally stops before the monitor loop, route completion, dropoff
verification, payload release, or any delivery completion claim.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import textwrap
import time
from typing import Any

from scripts import smoke_px4_gazebo_sitl_mission_upload as upload_smoke
from src.runtime.missionos_auto_mission_runner import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_DO_SET_MODE,
    MAV_RESULT_ACCEPTED,
    MISSIONOS_AUTO_MISSION_PHASE3B_LIVE_BOUNDARY_SUMMARY_SCHEMA_VERSION,
    MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
    MissionOSAutoMissionPhase3BLiveBoundarySummary,
    PX4_CUSTOM_MAIN_MODE_AUTO,
    PX4_CUSTOM_SUB_MODE_AUTO_MISSION,
    compile_operator_coordinate_route_auto_mission,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_CMD_NAV_LAND,
    MAV_MISSION_ACCEPTED,
)


OPT_IN_ENV = "RUN_MISSIONOS_AUTO_MISSION_PHASE3B_LIVE_BOUNDARY"
ROOT_DIR = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT_DIR / "output/missionos_auto_mission_runner/phase3b_live_boundary"
PX4_NAVIGATION_STATE_AUTO_MISSION = 3
PX4_ARMING_STATE_ARMED = 2
PX4_LANDED_STATE_IN_AIR = 2

PHASE3B_PROBE_ROUTE_M = 60.0


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run the Phase 3B live boundary.")


def _run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUN_ROOT / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _docker_logs() -> str:
    return upload_smoke._run(
        ["docker", "logs", upload_smoke.CONTAINER_NAME],
        check=False,
        timeout=30,
    ).stdout


def _parse_int(text: str, field: str) -> int | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(-?\d+)", text)
    return int(match.group(1)) if match else None


def _parse_float(text: str, field: str) -> float | None:
    match = re.search(rf"\b{re.escape(field)}:\s*(-?\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def _local_xy_progress_m(before: str, after: str) -> float:
    before_x = _parse_float(before, "x") or 0.0
    before_y = _parse_float(before, "y") or 0.0
    after_x = _parse_float(after, "x") or before_x
    after_y = _parse_float(after, "y") or before_y
    return math.hypot(after_x - before_x, after_y - before_y)


def _docker_exec_px4_listener(topic: str, count: int = 1) -> str:
    result = upload_smoke._run(
        [
            "docker",
            "exec",
            upload_smoke.CONTAINER_NAME,
            "/opt/px4-gazebo/bin/px4-listener",
            topic,
            str(count),
        ],
        check=False,
        timeout=20,
    )
    return (result.stdout + result.stderr).strip()


def _phase3b_probe_route_from_local_position(local_position: str) -> dict[str, Any]:
    ref_lat = _parse_float(local_position, "ref_lat")
    ref_lon = _parse_float(local_position, "ref_lon")
    if ref_lat is None or ref_lon is None:
        raise RuntimeError("PX4 local position did not expose ref_lat/ref_lon")
    # Keep the Phase 3B live probe near PX4 home. This smoke proves mode
    # transition and immediate abort, not long-route delivery.
    dropoff_lat = ref_lat + (PHASE3B_PROBE_ROUTE_M / 111_320.0)
    return {
        "schema_version": "mission_designer_coordinate_pair_route.v1",
        "route_id": "mission_designer_coordinate_pair_route_phase3b_live_probe",
        "takeoff_latitude": ref_lat,
        "takeoff_longitude": ref_lon,
        "dropoff_latitude": dropoff_lat,
        "dropoff_longitude": ref_lon,
        "dropoff_roof_height_agl_m": 10.0,
        "derived_route_distance_m": PHASE3B_PROBE_ROUTE_M,
    }


def _inner_phase3b_probe_script() -> str:
    arm_params = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    auto_params = [
        float(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
        float(PX4_CUSTOM_MAIN_MODE_AUTO),
        float(PX4_CUSTOM_SUB_MODE_AUTO_MISSION),
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    land_params = [0.0, 0.0, 0.0, 0.0, "nan", "nan", 0.0]
    return textwrap.dedent(
        f"""
        import json, math, socket, struct, subprocess, time
        MAVLINK2_MAGIC=0xFD
        MAVLINK_MSG_ID_HEARTBEAT=0
        MAVLINK_MSG_ID_COMMAND_LONG=76
        MAVLINK_MSG_ID_COMMAND_ACK=77
        CRC_EXTRA={{0:50,76:152,77:143}}
        PX4_MAVLINK_PORT={upload_smoke.PX4_MAVLINK_PORT}
        GCS_MAVLINK_PORT={upload_smoke.GCS_MAVLINK_PORT}

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

        def decode(data):
            if len(data)<12 or data[0]!=MAVLINK2_MAGIC: return None
            l=data[1]; mid=data[7]|(data[8]<<8)|(data[9]<<16)
            return mid, data[10:10+l]

        def command_long(command_id, params, seq):
            resolved=[]
            for value in params:
                resolved.append(math.nan if value == 'nan' else float(value))
            payload=struct.pack('<fffffffHBBB', *resolved, int(command_id), 1, 1, 0)
            return frame(MAVLINK_MSG_ID_COMMAND_LONG, payload, seq)

        def listener(topic, count=1):
            result=subprocess.run(
                ['/opt/px4-gazebo/bin/px4-listener', topic, str(count)],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            return (result.stdout + result.stderr).strip()

        def parse_bool(text, field):
            import re
            m=re.search(r'\\b'+re.escape(field)+r':\\s*(True|False)', text)
            return (m.group(1) == 'True') if m else None

        def send_command(sock, remote, command_id, params, seq, timeout_seconds):
            deadline=time.monotonic()+float(timeout_seconds)
            sent=False
            ack=None
            while time.monotonic()<deadline:
                sock.sendto(heartbeat(seq), remote); seq+=1
                if not sent:
                    sock.sendto(command_long(command_id, params, seq), remote); seq+=1
                    sent=True
                try:
                    data,_addr=sock.recvfrom(4096)
                except socket.timeout:
                    continue
                decoded=decode(data)
                if not decoded: continue
                mid,payload=decoded
                if mid==MAVLINK_MSG_ID_COMMAND_ACK and len(payload)>=3:
                    ack_command=struct.unpack('<H', payload[:2])[0]
                    result=int(payload[2])
                    if ack_command == int(command_id):
                        ack=result
                        break
            return {{'command_id': int(command_id), 'attempted': sent, 'ack_observed': ack is not None, 'ack_result': ack, 'next_seq': seq}}

        def wait_preflight_ready(sock, remote, seq):
            samples=[]
            deadline=time.monotonic()+20.0
            while time.monotonic()<deadline:
                sock.sendto(heartbeat(seq), remote); seq+=1
                status=listener('vehicle_status', 1)
                samples.append(status)
                if parse_bool(status, 'pre_flight_checks_pass') is True and parse_bool(status, 'gcs_connection_lost') is False:
                    return {{'ready': True, 'samples': samples, 'next_seq': seq, 'status': status}}
                time.sleep(0.5)
            return {{'ready': False, 'samples': samples, 'next_seq': seq, 'status': samples[-1] if samples else ''}}

        def wait_nav_state(sock, remote, seq, expected_nav_state, timeout_seconds):
            samples=[]
            deadline=time.monotonic()+float(timeout_seconds)
            while time.monotonic()<deadline:
                sock.sendto(heartbeat(seq), remote); seq+=1
                status=listener('vehicle_status', 1)
                samples.append(status)
                if parse_int(status, 'nav_state') == int(expected_nav_state):
                    return {{'observed': True, 'samples': samples, 'next_seq': seq, 'status': status}}
                time.sleep(0.2)
            return {{'observed': False, 'samples': samples, 'next_seq': seq, 'status': samples[-1] if samples else ''}}

        def parse_int(text, field):
            import re
            m=re.search(r'\\b'+re.escape(field)+r':\\s*(-?\\d+)', text)
            return int(m.group(1)) if m else None

        samples=[]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.2)
            sock.bind(('127.0.0.1', GCS_MAVLINK_PORT))
            remote=('127.0.0.1', PX4_MAVLINK_PORT)
            seq=80
            before_status=listener('vehicle_status', 1)
            before_local=listener('vehicle_local_position', 1)
            preflight=wait_preflight_ready(sock, remote, seq)
            seq=preflight['next_seq']
            arm=send_command(sock, remote, {MAV_CMD_COMPONENT_ARM_DISARM}, {arm_params!r}, seq, 8.0)
            seq=arm['next_seq']
            time.sleep(0.5)
            after_arm_status=listener('vehicle_status', 1)
            mode=send_command(sock, remote, {MAV_CMD_DO_SET_MODE}, {auto_params!r}, seq, 8.0)
            seq=mode['next_seq']
            nav_wait=wait_nav_state(sock, remote, seq, {PX4_NAVIGATION_STATE_AUTO_MISSION}, 1.0)
            seq=nav_wait['next_seq']
            after_mode_status=nav_wait['status'] or listener('vehicle_status', 1)
            land=send_command(sock, remote, {MAV_CMD_NAV_LAND}, {land_params!r}, seq, 8.0)
            seq=land['next_seq']
            after_land_status=listener('vehicle_status', 1)
            after_land_local=listener('vehicle_local_position', 1)
            for _ in range(20):
                status=listener('vehicle_status', 1)
                local=listener('vehicle_local_position', 1)
                samples.append({{'vehicle_status': status, 'vehicle_local_position': local}})
                arming=parse_int(status, 'arming_state')
                landed=parse_int(status, 'landed_state')
                if arming != {PX4_ARMING_STATE_ARMED}:
                    break
                if landed is not None and landed != {PX4_LANDED_STATE_IN_AIR}:
                    disarm=send_command(sock, remote, {MAV_CMD_COMPONENT_ARM_DISARM}, [0.0,0.0,0.0,0.0,0.0,0.0,0.0], seq, 5.0)
                    seq=disarm['next_seq']
                    time.sleep(1.0)
                    samples.append({{'vehicle_status': listener('vehicle_status', 1), 'vehicle_local_position': listener('vehicle_local_position', 1), 'disarm_command': disarm}})
                    break
                time.sleep(1.0)

        final_status=listener('vehicle_status', 1)
        final_local=listener('vehicle_local_position', 1)
        print(json.dumps({{
            'before_status': before_status,
            'before_local_position': before_local,
            'preflight_wait': preflight,
            'after_arm_status': after_arm_status,
            'after_mode_status': after_mode_status,
            'auto_mission_nav_wait': nav_wait,
            'after_land_status': after_land_status,
            'after_land_local_position': after_land_local,
            'final_status': final_status,
            'final_local_position': final_local,
            'arm_command': arm,
            'auto_mission_mode_command': mode,
            'land_abort_command': land,
            'post_abort_samples': samples,
        }}, sort_keys=True))
        """
    )


def _actual_phase3b_probe() -> dict[str, Any]:
    result = upload_smoke._run(
        ["docker", "exec", "-i", upload_smoke.CONTAINER_NAME, "python3", "-"],
        input_text=_inner_phase3b_probe_script(),
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "phase3b probe failed: " + result.stdout[-1000:] + result.stderr[-1000:]
        )
    if not result.stdout.strip():
        raise RuntimeError("phase3b probe produced no output: " + result.stderr[-500:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def _build_summary(
    *,
    upload_observed: dict[str, Any],
    probe_observed: dict[str, Any],
    mission_count: int,
) -> MissionOSAutoMissionPhase3BLiveBoundarySummary:
    after_mode_status = str(probe_observed.get("after_mode_status") or "")
    final_status = str(probe_observed.get("final_status") or "")
    before_local = str(probe_observed.get("before_local_position") or "")
    final_local = str(probe_observed.get("final_local_position") or "")
    arm = dict(probe_observed.get("arm_command") or {})
    mode = dict(probe_observed.get("auto_mission_mode_command") or {})
    land = dict(probe_observed.get("land_abort_command") or {})
    observed_nav_state = _parse_int(after_mode_status, "nav_state")
    arming_state_after_abort = _parse_int(final_status, "arming_state")
    landed_state_after_abort = _parse_int(final_status, "landed_state")
    nav_state_auto = observed_nav_state == PX4_NAVIGATION_STATE_AUTO_MISSION
    auto_started = (
        upload_observed.get("mission_ack_type") == MAV_MISSION_ACCEPTED
        and arm.get("ack_result") == MAV_RESULT_ACCEPTED
        and mode.get("ack_result") == MAV_RESULT_ACCEPTED
        and nav_state_auto
    )
    blocked: list[str] = []
    if upload_observed.get("mission_ack_type") != MAV_MISSION_ACCEPTED:
        blocked.append("mission_upload_not_accepted")
    if arm.get("ack_result") != MAV_RESULT_ACCEPTED:
        blocked.append("arm_ack_not_accepted")
    if mode.get("ack_result") != MAV_RESULT_ACCEPTED:
        blocked.append("auto_mission_mode_ack_not_accepted")
    if not nav_state_auto:
        blocked.append("auto_mission_nav_state_not_observed")
    if land.get("ack_result") != MAV_RESULT_ACCEPTED:
        blocked.append("land_abort_ack_not_accepted")
    disarm_observed = arming_state_after_abort != PX4_ARMING_STATE_ARMED
    if not disarm_observed:
        blocked.append("disarm_not_observed_after_abort")
    return MissionOSAutoMissionPhase3BLiveBoundarySummary(
        mission_upload_accepted=upload_observed.get("mission_ack_type")
        == MAV_MISSION_ACCEPTED,
        mission_count_sent=mission_count,
        mission_ack_observed=bool(upload_observed.get("mission_ack_observed")),
        mission_ack_result=upload_observed.get("mission_ack_type"),
        arm_command_ack_observed=bool(arm.get("ack_observed")),
        arm_command_ack_result=arm.get("ack_result"),
        auto_mission_mode_ack_observed=bool(mode.get("ack_observed")),
        auto_mission_mode_ack_result=mode.get("ack_result"),
        nav_state_auto_mission_observed=nav_state_auto,
        observed_nav_state=observed_nav_state,
        immediate_abort_ack_observed=bool(land.get("ack_observed")),
        immediate_abort_ack_result=land.get("ack_result"),
        disarm_observed=disarm_observed,
        arming_state_after_abort=arming_state_after_abort,
        landed_state_after_abort=landed_state_after_abort,
        observed_progress_m=round(_local_xy_progress_m(before_local, final_local), 3),
        auto_mission_started=auto_started,
        blocked_reasons=tuple(blocked),
    )


def main() -> int:
    _require_opt_in()
    run_dir = _run_dir()
    upload_smoke._start_container()
    try:
        route = _phase3b_probe_route_from_local_position(
            _docker_exec_px4_listener("vehicle_local_position", 1)
        )
        compilation = compile_operator_coordinate_route_auto_mission(route)
        upload_observed = upload_smoke._actual_upload(items=compilation.mission_items)
        probe_observed = _actual_phase3b_probe()
        summary = _build_summary(
            upload_observed=upload_observed,
            probe_observed=probe_observed,
            mission_count=len(compilation.mission_items),
        )
        payload = {
            "summary": summary.model_dump(mode="json"),
            "upload_observed": upload_observed,
            "probe_observed": probe_observed,
            "compilation": compilation.model_dump(mode="json"),
            "artifact_dir": str(run_dir),
        }
        (run_dir / "summary.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (run_dir / "px4_docker.log").write_text(_docker_logs(), encoding="utf-8")
        print(json.dumps(payload["summary"], indent=2, sort_keys=True))
        print("PHASE3B_LIVE_BOUNDARY_SUMMARY_JSON " + json.dumps(payload["summary"], sort_keys=True))
        assert (
            summary.schema_version
            == MISSIONOS_AUTO_MISSION_PHASE3B_LIVE_BOUNDARY_SUMMARY_SCHEMA_VERSION
        )
        assert summary.mission_upload_accepted is True
        assert summary.arm_command_ack_result == MAV_RESULT_ACCEPTED
        assert summary.auto_mission_mode_ack_result == MAV_RESULT_ACCEPTED
        assert summary.nav_state_auto_mission_observed is True
        assert summary.immediate_abort_ack_result == MAV_RESULT_ACCEPTED
        assert summary.disarm_observed is True
        assert summary.auto_mission_started is True
        assert summary.route_completed_claimed is False
        assert summary.delivery_completion_claimed is False
        return 0
    finally:
        upload_smoke._stop_container()


if __name__ == "__main__":
    raise SystemExit(main())
