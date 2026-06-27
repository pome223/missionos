#!/usr/bin/env python3
"""Opt-in fixture-backed Digital Twin world-bound SITL E2E smoke."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from scripts import smoke_digital_twin_px4_mission_upload as upload_smoke
from scripts import smoke_px4_gazebo_sitl_mission_upload as px4_upload_smoke
from src.runtime.digital_twin_mission_environment import (
    build_digital_twin_stage1_environment,
)
from src.runtime.digital_twin_sitl_execution_result import (
    DIGITAL_TWIN_SITL_EXECUTION_RESULT_SCHEMA_VERSION,
    build_digital_twin_sitl_execution_result,
    digital_twin_sitl_execution_result_ref,
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


OPT_IN_ENV = "RUN_DIGITAL_TWIN_WORLD_BOUND_SITL_E2E_SMOKE"
CONTAINER_NAME = "boiled-claw-digital-twin-world-bound-e2e"
PX4_GAZEBO_IMAGE = os.getenv(
    "PX4_GAZEBO_SITL_TELEMETRY_IMAGE",
    "px4io/px4-sitl-gazebo:latest",
)
ROOT_DIR = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT_DIR / "output/digital_twin/world_bound_sitl_e2e"
PROMPT = "10km先の3000mの山小屋に水3kgを届ける"
PROMPT_REF = "px4_gazebo_mission_prompt_request:digital_twin_world_bound_e2e"
NOW = datetime(2026, 5, 8, 2, 0, tzinfo=timezone.utc)


class _WorldBoundUploadShim:
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


class _ObservedWorldBoundUploader:
    def __init__(self, observed: dict[str, Any]):
        self.observed = observed
        self.heartbeat_observed = bool(observed.get("heartbeat_observed"))

    def upload(self, *, items, target_endpoint, timeout_seconds):
        return (
            tuple(int(item) for item in self.observed["mission_request_sequences"]),
            int(self.observed["mission_ack_type"]),
        )


def _run(command: list[str], *, check: bool = True, timeout: int = 120):
    return subprocess.run(
        command,
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def _require_opt_in() -> None:
    if os.getenv(OPT_IN_ENV) != "1":
        raise SystemExit(f"Set {OPT_IN_ENV}=1 to run Digital Twin world-bound SITL E2E.")


def _copy_px4_world_assets(run_dir: Path) -> Path:
    world_root = run_dir / "px4_world"
    if (world_root / "models/x500/model.sdf").exists():
        return world_root
    world_root.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{world_root.resolve()}:/out",
            PX4_GAZEBO_IMAGE,
            "-lc",
            (
                "rm -rf /out/models /out/worlds; "
                "mkdir -p /out/worlds; "
                "cp -a /opt/px4-gazebo/share/gz/models /out/models; "
                "cp /opt/px4-gazebo/share/gz/worlds/default.sdf /out/worlds/default.sdf"
            ),
        ],
        timeout=180,
    )
    return world_root


def _inject_digital_twin_terrain(world_root: Path, generated_world_path: Path) -> Path:
    generated_text = generated_world_path.read_text(encoding="utf-8")
    match = re.search(
        r'    <model name="digital_twin_heightmap_terrain">.*?\n    </model>',
        generated_text,
        re.S,
    )
    if not match:
        raise RuntimeError("generated Digital Twin world did not include terrain model")
    terrain_model = match.group(0)
    heightmap_uris = sorted(set(re.findall(r"<uri>([^<]+)</uri>", terrain_model)))
    heightmap_root = world_root / "heightmaps"
    heightmap_root.mkdir(parents=True, exist_ok=True)
    for uri in heightmap_uris:
        source = ROOT_DIR / uri
        if not source.exists():
            raise RuntimeError(f"Digital Twin heightmap URI missing: {uri}")
        shutil.copy2(source, heightmap_root / source.name)
        terrain_model = terrain_model.replace(uri, f"../heightmaps/{source.name}")
    default_world_path = world_root / "worlds/default.sdf"
    default_world = default_world_path.read_text(encoding="utf-8-sig")
    if "digital_twin_heightmap_terrain" not in default_world:
        default_world = default_world.replace("  </world>", terrain_model + "\n  </world>")
        default_world_path.write_text(default_world, encoding="utf-8")
    return default_world_path


def _wait_for_world_bound_startup(
    prepared_world_path: Path,
    timeout: float = 90.0,
) -> tuple[str, bool]:
    prepared_world_text = prepared_world_path.read_text(encoding="utf-8")
    terrain_model_injected = "digital_twin_heightmap_terrain" in prepared_world_text
    deadline = time.monotonic() + timeout
    logs = ""
    while time.monotonic() < deadline:
        logs = _run(["docker", "logs", "--tail", "360", CONTAINER_NAME], check=False).stdout
        if (
            terrain_model_injected
            and "Gazebo world is ready" in logs
            and "ODE Heightfield AABB" in logs
            and "Startup script returned successfully" in logs
        ):
            return logs, True
        if "Timed out waiting for Gazebo world" in logs or "Segmentation fault" in logs:
            return logs, False
        time.sleep(1.0)
    return logs, False


def _start_world_bound_container(
    world_root: Path,
    prepared_world_path: Path,
) -> tuple[list[str], int, str, bool]:
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)
    px4_home_env: list[str] = []
    for name in ("PX4_HOME_LAT", "PX4_HOME_LON", "PX4_HOME_ALT"):
        value = os.getenv(f"DIGITAL_TWIN_{name}")
        if value:
            px4_home_env.extend(["-e", f"{name}={value}"])
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "-e",
        "PX4_SIM_MODEL=gz_x500",
        "-e",
        "PX4_GZ_WORLD=default",
        "-e",
        "HEADLESS=1",
        "-e",
        "PX4_GZ_NO_FOLLOW=1",
        "-e",
        "PX4_GZ_WORLDS=/dt/worlds",
        "-e",
        "GZ_SIM_RESOURCE_PATH=/dt:/dt/models:/opt/px4-gazebo/share/gz/models",
        *px4_home_env,
        "-v",
        f"{world_root.resolve()}:/dt",
        "-v",
        f"{(world_root / 'models/x500').resolve()}:/opt/px4-gazebo/share/gz/models/x500:ro",
        PX4_GAZEBO_IMAGE,
        "-d",
    ]
    completed = _run(command, timeout=240)
    container_id = completed.stdout.strip()
    logs, started = _wait_for_world_bound_startup(
        prepared_world_path,
        float(os.getenv("DIGITAL_TWIN_WORLD_BOUND_STARTUP_TIMEOUT", "90"))
    )
    inspect = _run(
        ["docker", "inspect", "-f", "{{.State.Pid}}", CONTAINER_NAME],
        check=False,
    )
    pid = int(inspect.stdout.strip() or "0")
    return command, pid, logs, started


def _stop_world_bound_container() -> tuple[str, int]:
    logs = _run(["docker", "logs", CONTAINER_NAME], check=False).stdout
    inspect = _run(
        ["docker", "inspect", "-f", "{{.State.ExitCode}}", CONTAINER_NAME],
        check=False,
    )
    exit_code = int(inspect.stdout.strip() or "0") if inspect.stdout.strip() else 0
    _run(["docker", "rm", "-f", CONTAINER_NAME], check=False)
    return logs, exit_code


def run_smoke() -> dict[str, Any]:
    _require_opt_in()
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
    world_artifact = digital_twin["gazebo_world_artifact"]
    generated_world_path = ROOT_DIR / world_artifact["world_file_path_or_artifact_uri"]
    world_root = _copy_px4_world_assets(run_dir)
    prepared_world_path = _inject_digital_twin_terrain(world_root, generated_world_path)
    started_at = datetime.now(timezone.utc)
    command: list[str] = []
    pid = 0
    startup_logs = ""
    startup_ok = False
    upload_observed: dict[str, Any] | None = None
    stopped_at = None
    exit_code = 0
    try:
        command, pid, startup_logs, startup_ok = _start_world_bound_container(
            world_root,
            prepared_world_path,
        )
        if not startup_ok:
            raise RuntimeError("Digital Twin world-bound PX4/Gazebo startup failed")
        items = digital_twin_candidate_upload_items(
            digital_twin["digital_twin_px4_mission_item_candidate"]
        )
        upload_observed = upload_smoke._docker_exec_upload_with_heartbeat(
            _WorldBoundUploadShim,
            items,
        )
        if upload_observed.get("mission_ack_observed") is not True:
            raise RuntimeError("Digital Twin world-bound upload did not observe ACK")
    finally:
        final_logs, exit_code = _stop_world_bound_container()
        stopped_at = datetime.now(timezone.utc)
        (run_dir / "px4_gazebo.stdout.log").write_text(
            final_logs or startup_logs,
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
        px4_mission_item_candidate=digital_twin["digital_twin_px4_mission_item_candidate"],
        sitl_process_run=process_run,
        target_endpoint=DEFAULT_DIGITAL_TWIN_SITL_ENDPOINT,
        operator_approved=True,
        server_opt_in=True,
        same_run_binding_ref=(
            "digital_twin_sitl_binding_gate:"
            + digital_twin["digital_twin_sitl_binding_gate"]["gate_id"]
        ),
        uploader=_ObservedWorldBoundUploader(upload_observed or {}),
        timeout_seconds=5.0,
        now=NOW,
    )
    execution_result = build_digital_twin_sitl_execution_result(
        gazebo_world_artifact=world_artifact,
        coordinate_transform_candidate=digital_twin["coordinate_transform_candidate"],
        px4_mission_item_candidate=digital_twin["digital_twin_px4_mission_item_candidate"],
        sitl_binding_gate=digital_twin["digital_twin_sitl_binding_gate"],
        sitl_process_run=process_run,
        mission_upload_receipt=receipt,
        now=NOW,
    )
    summary = {
        "schema_version": execution_result.schema_version,
        "schema_version_expected": DIGITAL_TWIN_SITL_EXECUTION_RESULT_SCHEMA_VERSION,
        "execution_result_ref": digital_twin_sitl_execution_result_ref(execution_result),
        "execution_status": execution_result.execution_status,
        "world_artifact_load_mode": execution_result.world_artifact_load_mode,
        "px4_loaded_world_file_path": execution_result.px4_loaded_world_file_path,
        "world_bound": execution_result.world_bound,
        "terrain_artifact_used": execution_result.terrain_artifact_used,
        "prepared_px4_world_path": str(prepared_world_path),
        "generated_world_file_used": world_artifact["world_file_path_or_artifact_uri"],
        "gazebo_world_artifact_ref": execution_result.gazebo_world_artifact_ref,
        "process_run_ref": digital_twin_sitl_process_run_ref(process_run),
        "process_launch_attempted": process_run.process_launch_attempted,
        "gazebo_execution_invoked": process_run.gazebo_execution_invoked,
        "px4_process_invoked": process_run.px4_process_invoked,
        "mission_upload_receipt_ref": (
            execution_result.digital_twin_sitl_mission_upload_receipt_ref
        ),
        "mission_items_source": receipt.mission_items_source,
        "mission_upload_observed": execution_result.mission_upload_observed,
        "mission_ack_observed": execution_result.mission_ack_observed,
        "mission_ack_type": execution_result.mission_ack_type,
        "mission_request_sequences": list(receipt.mission_request_sequences),
        "heartbeat_observed": execution_result.heartbeat_observed,
        "flight_telemetry_observed": execution_result.flight_telemetry_observed,
        "payload_release_observed": execution_result.payload_release_observed,
        "dropoff_verified": execution_result.dropoff_verified,
        "observed_facts_only": execution_result.observed_facts_only,
        "hardware_target_allowed": execution_result.hardware_target_allowed,
        "physical_execution_invoked": execution_result.physical_execution_invoked,
        "approval_free_stronger_execution_allowed": (
            execution_result.approval_free_stronger_execution_allowed
        ),
        "blocked_reasons": list(execution_result.blocked_reasons),
        "execution_result_hash_equals_sha256": (
            execution_result.execution_result_hash == execution_result.sha256
        ),
    }
    assert summary["execution_status"] == "terrain_injected_world_upload_ack_telemetry_observed"
    assert summary["world_artifact_load_mode"] == "terrain_injection_into_default_world"
    assert summary["world_bound"] is False
    assert summary["terrain_artifact_used"] is True
    assert summary["process_launch_attempted"] is True
    assert summary["gazebo_execution_invoked"] is True
    assert summary["px4_process_invoked"] is True
    assert summary["mission_upload_observed"] is True
    assert summary["mission_ack_observed"] is True
    assert summary["heartbeat_observed"] is True
    assert summary["payload_release_observed"] is False
    assert summary["dropoff_verified"] is False
    assert summary["hardware_target_allowed"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["execution_result_hash_equals_sha256"] is True
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
