#!/usr/bin/env python3
"""Digital Twin SITL MAVLink upload smoke.

Default mode proves fail-closed behavior without dispatch. Real upload requires
RUN_DIGITAL_TWIN_SITL_MISSION_UPLOAD=1 and an allowlisted loopback PX4 SITL
endpoint or the docker-exec helper opt-in.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import textwrap
from typing import Any, Sequence

from src.runtime.digital_twin_mission_environment import (
    build_digital_twin_stage1_environment,
)
from src.runtime.digital_twin_sitl_mavlink_upload import (
    DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
    DIGITAL_TWIN_SITL_MISSION_UPLOAD_OPT_IN_ENV,
    build_digital_twin_sitl_mission_upload_receipt,
)
from src.runtime.digital_twin_sitl_process_runner import (
    run_digital_twin_sitl_process,
)
from src.runtime.px4_gazebo_sitl_mission_upload import PX4GazeboSITLMissionItem


NOW = datetime(2026, 5, 8, 1, 30, tzinfo=timezone.utc)
PROMPT_REF = "px4_gazebo_mission_prompt_request:digital_twin_upload_smoke"
DOCKER_EXEC_ENV = "RUN_DIGITAL_TWIN_SITL_DOCKER_EXEC_MISSION_UPLOAD"


class _DockerExecDigitalTwinUploader:
    heartbeat_observed: bool = False

    def upload(
        self,
        *,
        items: Sequence[PX4GazeboSITLMissionItem],
        target_endpoint: str,
        timeout_seconds: float,
    ) -> tuple[tuple[int, ...], int]:
        from scripts import smoke_px4_gazebo_sitl_mission_upload as upload_smoke

        upload_smoke._start_container()
        try:
            observed = _docker_exec_upload_with_heartbeat(upload_smoke, items)
        finally:
            upload_smoke._stop_container()
        if observed.get("mission_ack_observed") is not True:
            raise RuntimeError("docker exec Digital Twin upload did not observe ACK")
        self.heartbeat_observed = bool(observed.get("heartbeat_observed"))
        return (
            tuple(int(item) for item in observed["mission_request_sequences"]),
            int(observed["mission_ack_type"]),
        )


def _docker_exec_upload_with_heartbeat(upload_smoke, items) -> dict[str, Any]:
    mission_items_json = json.dumps(
        upload_smoke._mission_upload_item_tuples(items),
        sort_keys=True,
    )
    script = textwrap.dedent(f"""
        import json, socket, struct, subprocess, time
        MAVLINK2_MAGIC=0xFD
        CRC_EXTRA={{0:50,44:221,47:153,51:196,73:38,76:152}}
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
        def heartbeat(seq=0):
            return frame(0, struct.pack('<IBBBBB',0,6,8,0,4,3), seq)
        def request_heartbeat(seq=1):
            payload=struct.pack('<fffffffHBBB',0.0,0,0,0,0,0,0,512,1,1,0)
            return frame(76, payload, seq)
        def decode(data):
            if len(data)<12 or data[0]!=MAVLINK2_MAGIC: return None
            l=data[1]; mid=data[7]|(data[8]<<8)|(data[9]<<16)
            return mid, data[10:10+l]
        def mission_count(count, seq):
            return frame(44, struct.pack('<HBBB', count, 1, 1, 0), seq)
        def mission_item_int(seqno, command, lat, lon, alt, current, frame_kind, param1, param2, param3, param4, seq):
            payload=struct.pack('<ffffiifHHBBBBBB',float(param1),float(param2),float(param3),float(param4),int(lat*10000000),int(lon*10000000),float(alt),seqno,command,1,1,frame_kind,current,1,0)
            return frame(73, payload, seq)
        subprocess.run(['/opt/px4-gazebo/bin/px4-mavlink','start','-u','{upload_smoke.PX4_MAVLINK_PORT}','-r','400000','-t','127.0.0.1','-o','{upload_smoke.GCS_MAVLINK_PORT}','-m','onboard'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        items=[tuple(item) for item in json.loads({mission_items_json!r})]
        requests=[]; ack=None; seq=0; heartbeat_observed=False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(12)
            sock.bind(('127.0.0.1',{upload_smoke.GCS_MAVLINK_PORT}))
            sock.sendto(heartbeat(9), ('127.0.0.1',{upload_smoke.PX4_MAVLINK_PORT}))
            sock.sendto(request_heartbeat(10), ('127.0.0.1',{upload_smoke.PX4_MAVLINK_PORT}))
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                try: data,addr=sock.recvfrom(4096)
                except socket.timeout: break
                decoded=decode(data)
                if decoded and decoded[0] == 0:
                    heartbeat_observed=True
                    break
            sock.sendto(mission_count(len(items), seq), ('127.0.0.1',{upload_smoke.PX4_MAVLINK_PORT})); seq+=1
            deadline=time.monotonic()+12
            while time.monotonic()<deadline and ack is None:
                try: data,addr=sock.recvfrom(4096)
                except socket.timeout: break
                decoded=decode(data)
                if not decoded: continue
                mid,payload=decoded
                if mid==0:
                    heartbeat_observed=True
                elif mid==51 and len(payload)>=2:
                    rq=struct.unpack('<H',payload[:2])[0]
                    if rq < len(items):
                        requests.append(rq)
                        sock.sendto(mission_item_int(*items[rq], seq), ('127.0.0.1',{upload_smoke.PX4_MAVLINK_PORT})); seq+=1
                elif mid==47 and len(payload)>=3:
                    ack=payload[2]
                    break
        print(json.dumps({{'mission_items':items,'mission_request_sequences':requests,'mission_ack_type':ack,'mission_ack_observed':ack is not None,'heartbeat_observed':heartbeat_observed}}, sort_keys=True))
    """)
    result = upload_smoke._run(
        ["docker", "exec", "-i", upload_smoke.CONTAINER_NAME, "python3", "-"],
        input_text=script,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout[-500:] + result.stderr[-500:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def _summary(receipt) -> dict[str, Any]:
    return {
        "schema_version": receipt.schema_version,
        "receipt_ref": (
            f"digital_twin_sitl_mission_upload_receipt:{receipt.receipt_id}"
        ),
        "target_endpoint": receipt.target_endpoint,
        "mission_upload_attempted": receipt.mission_upload_attempted,
        "px4_mission_upload_allowed": receipt.px4_mission_upload_allowed,
        "mavlink_dispatch_performed": receipt.mavlink_dispatch_performed,
        "mission_upload_observed": receipt.mission_upload_observed,
        "mission_ack_observed": receipt.mission_ack_observed,
        "mission_ack_type": receipt.mission_ack_type,
        "mission_request_sequences": list(receipt.mission_request_sequences),
        "telemetry_observed": receipt.telemetry_observed,
        "heartbeat_observed": receipt.heartbeat_observed,
        "candidate_item_count": receipt.candidate_item_count,
        "mission_items_source": receipt.mission_items_source,
        "blocked_reasons": list(receipt.blocked_reasons),
        "hardware_target_allowed": receipt.hardware_target_allowed,
        "physical_execution_invoked": receipt.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": (
            receipt.approval_free_stronger_execution_allowed
        ),
        "receipt_hash_equals_sha256": receipt.receipt_hash == receipt.sha256,
    }


def main() -> dict[str, Any]:
    result = build_digital_twin_stage1_environment(
        prompt="10km先の3000mの山小屋に水3kgを届ける",
        prompt_request_ref=PROMPT_REF,
        altitude_target_m=3000,
        payload_weight_kg=3,
        weather_hazard_labels=(),
        now=NOW,
    )
    previous_command = os.environ.get("DIGITAL_TWIN_GZ_SIM_COMMAND")
    os.environ.setdefault("DIGITAL_TWIN_GZ_SIM_COMMAND", "/bin/sh -c 'sleep 1'")
    try:
        process_run = run_digital_twin_sitl_process(
            gazebo_world_artifact=result["gazebo_world_artifact"],
            startup_window_seconds=0.1,
            cleanup_timeout_seconds=1.0,
            now=NOW,
        )
    finally:
        if previous_command is None:
            os.environ.pop("DIGITAL_TWIN_GZ_SIM_COMMAND", None)

    operator_approved = os.getenv(DIGITAL_TWIN_SITL_MISSION_UPLOAD_OPT_IN_ENV) == "1"
    server_opt_in = operator_approved
    uploader = (
        _DockerExecDigitalTwinUploader()
        if os.getenv(DOCKER_EXEC_ENV) == "1"
        else None
    )
    receipt = build_digital_twin_sitl_mission_upload_receipt(
        px4_mission_item_candidate=result["digital_twin_px4_mission_item_candidate"],
        sitl_process_run=process_run,
        target_endpoint=os.getenv(
            "DIGITAL_TWIN_SITL_MISSION_UPLOAD_ENDPOINT",
            DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
        ),
        operator_approved=operator_approved,
        server_opt_in=server_opt_in,
        same_run_binding_ref=(
            "digital_twin_sitl_binding_gate:"
            + result["digital_twin_sitl_binding_gate"]["gate_id"]
        ),
        uploader=uploader,
        timeout_seconds=float(os.getenv("DIGITAL_TWIN_SITL_UPLOAD_TIMEOUT", "5")),
        now=NOW,
    )
    summary = _summary(receipt)
    if operator_approved:
        assert summary["mission_upload_attempted"] is True
        assert summary["mission_upload_observed"] is True
        assert summary["mission_ack_observed"] is True
        assert summary["mission_items_source"] == "candidate_derived"
    else:
        assert summary["mission_upload_attempted"] is False
        assert summary["px4_mission_upload_allowed"] is False
        assert "server_opt_in_missing" in summary["blocked_reasons"]
        assert summary["mission_items_source"] == "candidate_derived"
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    return summary


if __name__ == "__main__":
    output = main()
    print(json.dumps(output, indent=2, sort_keys=True))
    print("SMOKE_SUMMARY_JSON " + json.dumps(output, sort_keys=True))
