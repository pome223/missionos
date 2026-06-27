#!/usr/bin/env python3
"""Opt-in actual PX4/Gazebo SITL MAVLink mission upload smoke for #410."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
import subprocess
import textwrap
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from src.runtime.delivery_episode_review import build_delivery_episode_scorecard_review
from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_recovery_decision import (
    build_delivery_recovery_decision_from_episode_review,
)
from src.runtime.operator_minimal_delivery_simulation import (
    build_operator_minimal_delivery_simulation_status,
)
from src.runtime.px4_gazebo_bounded_simulation_runner import (
    build_px4_gazebo_bounded_simulation_run,
)
from src.runtime.px4_gazebo_mission_scenario_designer import (
    approve_px4_gazebo_mission_scenario_for_bounded_simulation,
    run_px4_gazebo_mission_scenario_designer,
)
from src.runtime.px4_gazebo_sitl_mission_upload import (
    MAV_MISSION_ACCEPTED,
    PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
    PX4_GAZEBO_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION,
    attach_px4_gazebo_sitl_mission_upload_receipt,
)
from src.runtime.px4_gazebo_telemetry import (
    build_px4_gazebo_hil_review_gate_smoke,
    sanitize_px4_gazebo_telemetry_sample,
)
from src.runtime.simulated_delivery_command import (
    SimulatedCommandCategory,
    build_simulated_command_approval,
    build_simulated_command_proposal,
    build_simulated_command_receipt,
    build_simulated_command_rehearsal_result,
    build_simulator_command_execution_preflight,
)
from src.runtime.simulated_delivery_episode import (
    build_simulated_delivery_episode_from_bounded_gazebo_run,
)
from src.runtime.task_store import TaskStore

OPT_IN_ENV = "RUN_PX4_GAZEBO_SITL_MISSION_UPLOAD_SMOKE"
ROOT_DIR = Path(__file__).resolve().parents[1]
CONTAINER_NAME = "boiled-claw-px4-gazebo-sitl-mission-upload-smoke"
PX4_GAZEBO_IMAGE = os.getenv(
    "PX4_GAZEBO_SITL_TELEMETRY_IMAGE", "px4io/px4-sitl-gazebo:latest"
)
PX4_MODEL = "gz_x500"
GAZEBO_WORLD = "default"
PX4_MAVLINK_PORT = 14604
GCS_MAVLINK_PORT = 14654
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class ObservedUploader:
    def __init__(self, *, mission_request_sequences: tuple[int, ...], ack_type: int):
        self.mission_request_sequences = mission_request_sequences
        self.ack_type = ack_type

    def upload(self, *, items, target_endpoint, timeout_seconds):
        return self.mission_request_sequences, self.ack_type


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"Set {OPT_IN_ENV}=1 to run the actual SITL mission upload smoke."
        )


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


def _logs(tail: str = "360") -> str:
    return _run(["docker", "logs", "--tail", tail, CONTAINER_NAME], check=False).stdout


def _wait_for_startup(timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last_logs = ""
    while time.monotonic() < deadline:
        logs = _logs()
        if (
            "Gazebo world is ready" in logs
            and "gz_bridge] world: default, model: x500_0" in logs
            and "Startup script returned successfully" in logs
        ):
            return
        last_logs = logs
        time.sleep(1)
    raise RuntimeError(
        "timed out waiting for PX4/Gazebo SITL startup: " + last_logs[-800:]
    )


def _start_container() -> None:
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)
    px4_home_env: list[str] = []
    for name in ("PX4_HOME_LAT", "PX4_HOME_LON", "PX4_HOME_ALT"):
        value = os.getenv(name)
        if value:
            px4_home_env.extend(["-e", f"{name}={value}"])
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            # Expose the PX4 SITL MAVLink telemetry port so the host-side
            # readiness probe (lsof / ss) can observe it after startup.
            "-p",
            "14540:14540/udp",
            "-e",
            f"PX4_SIM_MODEL={PX4_MODEL}",
            "-e",
            f"PX4_GZ_WORLD={GAZEBO_WORLD}",
            "-e",
            "HEADLESS=1",
            "-e",
            "PX4_GZ_NO_FOLLOW=1",
            *px4_home_env,
            PX4_GAZEBO_IMAGE,
            "-d",
        ],
        timeout=240,
    )
    _wait_for_startup()


def _stop_container() -> None:
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)


def _mission_upload_item_tuples(
    items: Sequence[Any] | None = None,
) -> tuple[tuple[int, int, float, float, float, int, int, float, float, float, float], ...]:
    if items is None:
        return (
            (0, 22, 35.681236, 139.767125, 15.0, 1, 6, 0.0, 0.0, 0.0, 0.0),
            (1, 16, 35.6853615, 139.7294155, 20.0, 0, 6, 0.0, 0.0, 0.0, 0.0),
            (2, 16, 35.689487, 139.691706, 30.0, 0, 6, 0.0, 0.0, 0.0, 0.0),
            (3, 21, 35.689487, 139.691706, 0.0, 0, 6, 0.0, 0.0, 0.0, 0.0),
        )
    resolved: list[tuple[int, int, float, float, float, int, int, float, float, float, float]] = []
    for item in items:
        if isinstance(item, Mapping):
            value = item
        elif hasattr(item, "model_dump"):
            value = item.model_dump(mode="json")
        else:
            value = {
                "seq": getattr(item, "seq"),
                "command": getattr(item, "command"),
                "latitude_deg": getattr(item, "latitude_deg"),
                "longitude_deg": getattr(item, "longitude_deg"),
                "altitude_m": getattr(item, "altitude_m"),
                "current": getattr(item, "current", 0),
                "frame": getattr(item, "frame", 6),
                "param1": getattr(item, "param1", 0.0),
                "param2": getattr(item, "param2", 0.0),
                "param3": getattr(item, "param3", 0.0),
                "param4": getattr(item, "param4", 0.0),
            }
        resolved.append(
            (
                int(value["seq"]),
                int(value["command"]),
                float(value["latitude_deg"]),
                float(value["longitude_deg"]),
                float(value["altitude_m"]),
                int(value.get("current", 0)),
                int(value.get("frame", 6)),
                float(value.get("param1", 0.0)),
                float(value.get("param2", 0.0)),
                float(value.get("param3", 0.0)),
                float(value.get("param4", 0.0)),
            )
        )
    return tuple(resolved)


def _inner_upload_script(items: Sequence[Any] | None = None) -> str:
    mission_items_json = json.dumps(_mission_upload_item_tuples(items), sort_keys=True)
    return textwrap.dedent(f"""
        import json, socket, struct, subprocess, time
        MAVLINK2_MAGIC=0xFD
        MAVLINK_MSG_ID_MISSION_CLEAR_ALL=45
        MAVLINK_MSG_ID_MISSION_ACK=47
        MAVLINK_MSG_ID_MISSION_REQUEST_INT=51
        CRC_EXTRA={{0:50,44:221,45:232,47:153,51:196,73:38}}
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
        def decode(data):
            if len(data)<12 or data[0]!=MAVLINK2_MAGIC: return None
            l=data[1]; mid=data[7]|(data[8]<<8)|(data[9]<<16)
            return mid, data[10:10+l]
        def mission_count(count, seq):
            return frame(44, struct.pack('<HBBB', count, 1, 1, 0), seq)
        def mission_clear_all(seq):
            return frame(MAVLINK_MSG_ID_MISSION_CLEAR_ALL, struct.pack('<BBB', 1, 1, 0), seq)
        def mission_item_int(seqno, command, lat, lon, alt, current, frame_kind, param1, param2, param3, param4, seq):
            payload=struct.pack('<ffffiifHHBBBBBB',float(param1),float(param2),float(param3),float(param4),int(lat*10000000),int(lon*10000000),float(alt),seqno,command,1,1,frame_kind,current,1,0)
            return frame(73, payload, seq)
        subprocess.run(['/opt/px4-gazebo/bin/px4-mavlink','stop-all'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        start_result=subprocess.run(['/opt/px4-gazebo/bin/px4-mavlink','start','-u','{PX4_MAVLINK_PORT}','-r','400000','-t','127.0.0.1','-o','{GCS_MAVLINK_PORT}','-m','onboard'], check=False, text=True, capture_output=True)
        time.sleep(1.0)
        items=[tuple(item) for item in json.loads({mission_items_json!r})]
        requests=[]; ack=None; clear_ack=None; seq=0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(12)
            sock.bind(('127.0.0.1',{GCS_MAVLINK_PORT}))
            sock.sendto(mission_clear_all(seq), ('127.0.0.1',{PX4_MAVLINK_PORT})); seq+=1
            clear_deadline=time.monotonic()+3
            while time.monotonic()<clear_deadline and clear_ack is None:
                try: data,addr=sock.recvfrom(4096)
                except socket.timeout: break
                decoded=decode(data)
                if not decoded: continue
                mid,payload=decoded
                if mid==MAVLINK_MSG_ID_MISSION_ACK and len(payload)>=3:
                    clear_ack=payload[2]
                    break
            sock.sendto(mission_count(len(items), seq), ('127.0.0.1',{PX4_MAVLINK_PORT})); seq+=1
            deadline=time.monotonic()+12
            while time.monotonic()<deadline and ack is None:
                try: data,addr=sock.recvfrom(4096)
                except socket.timeout: break
                decoded=decode(data)
                if not decoded: continue
                mid,payload=decoded
                if mid==MAVLINK_MSG_ID_MISSION_REQUEST_INT and len(payload)>=2:
                    rq=struct.unpack('<H',payload[:2])[0]
                    if rq < len(items):
                        requests.append(rq)
                        sock.sendto(mission_item_int(*items[rq], seq), ('127.0.0.1',{PX4_MAVLINK_PORT})); seq+=1
                elif mid==MAVLINK_MSG_ID_MISSION_ACK and len(payload)>=3:
                    ack=payload[2]
                    break
        print(json.dumps({{'mission_items':items,'mission_request_sequences':requests,'mission_ack_type':ack,'mission_ack_observed':ack is not None,'mission_clear_all_ack_type':clear_ack,'mavlink_start_returncode':start_result.returncode,'mavlink_start_stderr_tail':start_result.stderr[-500:]}}, sort_keys=True))
    """)


def _actual_upload(items: Sequence[Any] | None = None) -> dict[str, Any]:
    result = _run(
        ["docker", "exec", "-i", CONTAINER_NAME, "python3", "-"],
        input_text=_inner_upload_script(items),
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "mission upload script failed: "
            + result.stdout[-1000:]
            + result.stderr[-1000:]
        )
    if not result.stdout.strip():
        raise RuntimeError(
            "mission upload script produced no output: " + result.stderr[-500:]
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _contract():
    return build_delivery_mission_contract(
        mission_id="sitl-mission-upload-smoke",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "latitude": 35.689487,
            "longitude": 139.691706,
            "altitude_m": 30.0,
        },
        delivery_window={
            "earliest_pickup_at": "2026-01-01T12:00:00Z",
            "latest_dropoff_at": "2026-01-01T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-sitl-upload", "max_weight_kg": 1.0},
        weather_constraints={
            "max_wind_speed_mps": 6.0,
            "max_precipitation_mm_per_hour": 0.0,
            "min_visibility_m": 1500.0,
        },
        battery_policy={
            "minimum_takeoff_percent": 80,
            "return_to_home_percent": 35,
            "reserve_landing_percent": 25,
        },
        landing_zone_policy={
            "min_clear_radius_m": 3.0,
            "max_slope_degrees": 5.0,
            "accepted_surface_kinds": ["marked_pad"],
        },
        telemetry_requirements={
            "required_measurements": [
                "position",
                "battery_percent",
                "vehicle_health",
                "weather_snapshot",
            ],
            "max_freshness_seconds": 2.0,
        },
        now=NOW,
    )


def _dropoff_evidence() -> dict[str, Any]:
    return {
        "evidence_ref": "simulated_dropoff_evidence:dropoff-pad-b",
        "dropoff_verified": True,
        "landing_error_m": 0.18,
    }


def _ready_preflight_chain(contract) -> dict[str, Any]:
    designed = run_px4_gazebo_mission_scenario_designer(
        prompt="標高30mの配送地点に1kgの荷物を届ける",
        now=NOW,
    )
    approved = approve_px4_gazebo_mission_scenario_for_bounded_simulation(
        proposal=designed["scenario_proposal"],
        validation=designed["validation_result"],
        now=NOW,
    )
    request = approved["bounded_simulation_request"]
    telemetry = sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": "sitl-upload-smoke-telemetry",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "sitl-upload-smoke",
                "vehicle_id": "vehicle-sitl-upload-smoke",
            },
            "captured_at": "2026-01-01T12:00:00Z",
            "telemetry": {
                "position": "35.689487,139.691706,0.18",
                "battery_percent": 88.0,
                "vehicle_health": "nominal",
                "weather_snapshot": "clear",
                "landing_zone_available": True,
            },
        }
    )
    hil_gate = build_px4_gazebo_hil_review_gate_smoke(
        telemetry,
        freshness_threshold_seconds=60.0,
        now=NOW,
    )
    telemetry_ref = f"px4_gazebo_sanitized_telemetry:{telemetry.telemetry_id}"
    hil_ref = f"hil_telemetry_review:{hil_gate['hil_telemetry_review']['review_id']}"
    gate_ref = f"autonomy_gate_result:{hil_gate['autonomy_gate_result']['gate_id']}"
    run = build_px4_gazebo_bounded_simulation_run(
        request=request,
        started_at=NOW,
        finished_at=NOW,
        max_duration_seconds=300,
        max_log_lines=260,
        observed_log_line_count=34,
        telemetry_captured_at=NOW,
        max_telemetry_age_seconds=300,
        telemetry_age_seconds=0.0,
        telemetry_refs=(telemetry_ref,),
        gate_ref=gate_ref,
        hil_review_ref=hil_ref,
        provenance={
            "world_name": "empty",
            "world_ref": "/tmp/empty.sdf",
            "world_sdf_path": "/tmp/empty.sdf",
            "network_mode": "none",
            "read_only_rootfs": True,
            "privileged": False,
            "cap_drop": ["ALL"],
        },
    )
    episode_artifacts = build_simulated_delivery_episode_from_bounded_gazebo_run(
        delivery_mission_contract=contract,
        bounded_simulation_request=request,
        bounded_simulation_run=run,
        sanitized_telemetry=telemetry,
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=hil_gate["autonomy_gate_result"],
        dropoff_evidence=_dropoff_evidence(),
        now=NOW,
    )
    reviewed = build_delivery_episode_scorecard_review(
        delivery_mission_contract=contract,
        simulated_delivery_episode=episode_artifacts["simulated_delivery_episode"],
        delivery_replay_trace=episode_artifacts["delivery_replay_trace"],
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=hil_gate["autonomy_gate_result"],
        sanitized_telemetry=telemetry,
        now=NOW,
    )
    decision = build_delivery_recovery_decision_from_episode_review(
        delivery_mission_contract=contract,
        simulated_delivery_episode=episode_artifacts["simulated_delivery_episode"],
        delivery_scorecard=reviewed["delivery_scorecard"],
        delivery_episode_review=reviewed["delivery_episode_review"],
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=hil_gate["autonomy_gate_result"],
        now=NOW,
    )
    operator_status = build_operator_minimal_delivery_simulation_status(
        delivery_mission_contract=contract,
        simulated_delivery_episode=episode_artifacts["simulated_delivery_episode"],
        delivery_scorecard=reviewed["delivery_scorecard"],
        delivery_episode_review=reviewed["delivery_episode_review"],
        delivery_recovery_decision=decision,
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=hil_gate["autonomy_gate_result"],
        now=NOW,
    )["operator_minimal_delivery_simulation_status"]
    proposal = build_simulated_command_proposal(
        delivery_mission_contract=contract,
        simulated_delivery_episode=episode_artifacts["simulated_delivery_episode"],
        delivery_scorecard=reviewed["delivery_scorecard"],
        delivery_episode_review=reviewed["delivery_episode_review"],
        delivery_recovery_decision=decision,
        operator_minimal_delivery_simulation_status=operator_status,
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=hil_gate["autonomy_gate_result"],
        command_category=SimulatedCommandCategory.START_SIMULATED_DELIVERY,
        now=NOW,
    )
    approval = build_simulated_command_approval(
        simulated_command_proposal=proposal,
        now=NOW,
    )
    receipt = build_simulated_command_receipt(
        simulated_command_proposal=proposal,
        simulated_command_approval=approval,
        now=NOW,
    )
    rehearsal = build_simulated_command_rehearsal_result(
        simulated_command_proposal=proposal,
        simulated_command_approval=approval,
        bounded_simulation_request=request,
        bounded_simulation_run=run,
        simulated_delivery_episode=episode_artifacts["simulated_delivery_episode"],
        delivery_recovery_decision=decision,
        operator_minimal_delivery_simulation_status=operator_status,
        now=NOW,
    )
    preflight = build_simulator_command_execution_preflight(
        simulated_command_proposal=proposal,
        simulated_command_approval=approval,
        simulated_command_receipt=receipt,
        simulated_command_rehearsal_result=rehearsal,
        bounded_simulation_run=run,
        simulated_delivery_episode=episode_artifacts["simulated_delivery_episode"],
        delivery_scorecard=reviewed["delivery_scorecard"],
        delivery_episode_review=reviewed["delivery_episode_review"],
        delivery_recovery_decision=decision,
        operator_minimal_delivery_simulation_status=operator_status,
        hil_telemetry_review=hil_gate["hil_telemetry_review"],
        autonomy_gate_result=hil_gate["autonomy_gate_result"],
        now=NOW,
    )
    return {
        "proposal": proposal,
        "approval": approval,
        "preflight": preflight,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    _require_opt_in()
    _start_container()
    try:
        observed = _actual_upload()
        if observed.get("mission_ack_type") != MAV_MISSION_ACCEPTED:
            raise RuntimeError("MISSION_ACK not accepted: " + json.dumps(observed))
        contract = _contract()
        chain = _ready_preflight_chain(contract)
        with TemporaryDirectory() as tmp:
            store = TaskStore(f"{tmp}/tasks.db")
            task = store.create(
                kind="control_supervisor",
                title="SITL mission upload smoke",
                status="running",
                artifacts={"existing": {"kept": True}},
            )
            attached = attach_px4_gazebo_sitl_mission_upload_receipt(
                task_id=task["task_id"],
                delivery_mission_contract=contract,
                simulator_command_execution_preflight=chain["preflight"],
                simulated_command_proposal=chain["proposal"],
                simulated_command_approval=chain["approval"],
                target_endpoint=PX4_GAZEBO_SITL_MISSION_UPLOAD_ENDPOINT,
                allow_sitl_mission_upload=True,
                geofence_radius_m=20_000.0,
                uploader=ObservedUploader(
                    mission_request_sequences=tuple(
                        observed["mission_request_sequences"]
                    ),
                    ack_type=int(observed["mission_ack_type"]),
                ),
                task_store_factory=lambda: store,
            )
            stored = store.get(task["task_id"])
        receipt = attached["px4_gazebo_sitl_mission_upload_receipt"]
        summary = {
            "schema_version": receipt["schema_version"],
            "upload_status": receipt["upload_status"],
            "target_endpoint": receipt["target_endpoint"],
            "mission_item_count": receipt["mission_item_count"],
            "mission_request_sequences": receipt["mission_request_sequences"],
            "mission_ack_type": receipt["mission_ack_type"],
            "mission_ack_observed": receipt["mission_ack_observed"],
            "external_dispatch_performed": receipt["external_dispatch_performed"],
            "mavlink_dispatch_performed": receipt["mavlink_dispatch_performed"],
            "px4_mission_upload_performed": receipt["px4_mission_upload_performed"],
            "hardware_target_allowed": receipt["hardware_target_allowed"],
            "physical_execution_invoked": receipt["physical_execution_invoked"],
            "gazebo_entity_mutation_performed": receipt[
                "gazebo_entity_mutation_performed"
            ],
            "task_status": stored["status"] if stored else None,
            "existing_artifact_kept": bool(
                stored and stored["artifacts"]["existing"]["kept"]
            ),
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        print("SMOKE_SUMMARY_JSON " + json.dumps(summary, sort_keys=True))
        assert (
            summary["schema_version"]
            == PX4_GAZEBO_SITL_MISSION_UPLOAD_RECEIPT_SCHEMA_VERSION
        )
        assert summary["upload_status"] == "uploaded"
        assert summary["external_dispatch_performed"] is True
        assert summary["mavlink_dispatch_performed"] is True
        assert summary["px4_mission_upload_performed"] is True
        assert summary["hardware_target_allowed"] is False
        assert summary["physical_execution_invoked"] is False
        assert summary["gazebo_entity_mutation_performed"] is False
        return 0
    finally:
        if not args.keep_running:
            _stop_container()


if __name__ == "__main__":
    raise SystemExit(main())
