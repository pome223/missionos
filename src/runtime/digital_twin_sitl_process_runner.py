"""Execution-first runner for generated Digital Twin Gazebo worlds."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import time
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.runtime.digital_twin_mission_environment import (
    DigitalTwinMissionEnvironmentError,
    GazeboWorldArtifact,
    gazebo_world_artifact_ref,
)


DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION = "digital_twin_sitl_process_run.v1"
DIGITAL_TWIN_SITL_RUN_ROOT = Path("output/digital_twin/sitl_runs")
DIGITAL_TWIN_WORLD_ROOT = Path("output/digital_twin/worlds")
DEFAULT_GAZEBO_DOCKER_IMAGE = "ghcr.io/openrobotics/gazebo:harmonic-full"


def _utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _content_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _text_tuple(value: Sequence[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(item) for item in value)


class DigitalTwinSITLProcessRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION] = (
        DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION
    )
    process_run_id: str
    gazebo_world_artifact_ref: str
    world_file_path_or_artifact_uri: str
    world_file_sha256: str
    process_launch_attempted: bool
    gazebo_execution_invoked: bool
    px4_process_invoked: bool
    process_pids: tuple[int, ...]
    command: tuple[str, ...]
    world_artifact_load_mode: Literal[
        "direct_world_artifact_load",
        "terrain_injection_into_default_world",
    ] = "direct_world_artifact_load"
    px4_loaded_world_file_path: str = ""
    started_at: datetime
    stopped_at: datetime | None = None
    exit_status: Literal[
        "not_attempted",
        "startup_failed",
        "exited",
        "terminated_after_startup_window",
        "killed_after_cleanup_timeout",
    ]
    exit_code: int | None = None
    stdout_ref: str
    stderr_ref: str
    startup_error_observed: bool = False
    simulation_only: Literal[True] = True
    hardware_target_allowed: Literal[False] = False
    physical_execution_invoked: Literal[False] = False
    approval_free_stronger_execution_allowed: Literal[False] = False
    run_hash: str
    sha256: str
    blocked_reasons: tuple[str, ...] = ()

    @field_validator("process_pids", "command", "blocked_reasons", mode="before")
    @classmethod
    def _coerce_tuple(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value or ())

    @field_validator("started_at", "stopped_at", mode="before")
    @classmethod
    def _coerce_time(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _utc(value)
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))

    @model_validator(mode="after")
    def _validate_process_run(self) -> "DigitalTwinSITLProcessRun":
        if not self.gazebo_world_artifact_ref.startswith("gazebo_world_artifact:"):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL process run requires world artifact ref"
            )
        if not self.world_file_path_or_artifact_uri.replace("\\", "/").startswith(
            "output/digital_twin/worlds/"
        ):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL process world path must stay under output/digital_twin/worlds/"
            )
        if not self.world_file_path_or_artifact_uri.endswith(".world.sdf"):
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL process world path must end with .world.sdf"
            )
        if self.process_launch_attempted:
            if not self.command:
                raise DigitalTwinMissionEnvironmentError(
                    "attempted Digital Twin SITL process run requires command"
                )
            if not self.process_pids:
                raise DigitalTwinMissionEnvironmentError(
                    "attempted Digital Twin SITL process run requires process pid"
                )
            if not self.gazebo_execution_invoked:
                raise DigitalTwinMissionEnvironmentError(
                    "attempted Digital Twin SITL process run must invoke Gazebo"
                )
            command_text = " ".join(self.command)
            world_stem = Path(self.world_file_path_or_artifact_uri).stem
            if self.world_artifact_load_mode == "direct_world_artifact_load":
                if self.px4_process_invoked and (
                    self.world_file_path_or_artifact_uri not in command_text
                    and world_stem not in command_text
                ):
                    raise DigitalTwinMissionEnvironmentError(
                        "direct Digital Twin SITL process command must reference world artifact path"
                    )
                if self.px4_loaded_world_file_path and (
                    self.px4_loaded_world_file_path
                    != self.world_file_path_or_artifact_uri
                ):
                    raise DigitalTwinMissionEnvironmentError(
                        "direct Digital Twin SITL process loaded world path must match artifact"
                    )
            else:
                if not self.px4_loaded_world_file_path:
                    raise DigitalTwinMissionEnvironmentError(
                        "terrain-injected Digital Twin SITL process requires PX4 loaded world path"
                    )
                if not self.px4_loaded_world_file_path.endswith(".sdf"):
                    raise DigitalTwinMissionEnvironmentError(
                        "terrain-injected Digital Twin SITL loaded world must be SDF"
                    )
                loaded_world_name = Path(self.px4_loaded_world_file_path).stem
                if (
                    loaded_world_name not in command_text
                    and f"PX4_GZ_WORLD={loaded_world_name}" not in command_text
                ):
                    raise DigitalTwinMissionEnvironmentError(
                        "terrain-injected Digital Twin SITL command must reference loaded PX4 world"
                    )
        if self.gazebo_execution_invoked and not self.process_launch_attempted:
            raise DigitalTwinMissionEnvironmentError(
                "Gazebo execution cannot be invoked without process launch attempt"
            )
        if self.exit_status == "not_attempted" and self.process_launch_attempted:
            raise DigitalTwinMissionEnvironmentError(
                "attempted Digital Twin SITL process run cannot be not_attempted"
            )
        if self.startup_error_observed and "gazebo_startup_error_observed" not in set(
            self.blocked_reasons
        ):
            raise DigitalTwinMissionEnvironmentError(
                "startup-error Digital Twin SITL process run requires blocked reason"
            )
        if self.run_hash != self.sha256:
            raise DigitalTwinMissionEnvironmentError(
                "Digital Twin SITL process run hash mismatch"
            )
        return self


def digital_twin_sitl_process_run_ref(run: DigitalTwinSITLProcessRun) -> str:
    return f"digital_twin_sitl_process_run:{run.process_run_id}"


def _docker_container_name(process_run_id: str) -> str:
    return f"boiled-claw-dt-{process_run_id[-12:]}"


def resolve_digital_twin_world_path(
    gazebo_world_artifact: GazeboWorldArtifact | Mapping[str, Any],
    *,
    repo_root: str | Path = ".",
) -> Path:
    artifact = (
        gazebo_world_artifact
        if isinstance(gazebo_world_artifact, GazeboWorldArtifact)
        else GazeboWorldArtifact.model_validate(gazebo_world_artifact)
    )
    raw_path = Path(artifact.world_file_path_or_artifact_uri)
    if raw_path.is_absolute():
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin world artifact path must be repo-relative"
        )
    normalized = Path(*raw_path.parts)
    if not normalized.as_posix().startswith(DIGITAL_TWIN_WORLD_ROOT.as_posix() + "/"):
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin world artifact path must stay under output/digital_twin/worlds/"
        )
    if normalized.suffixes[-2:] != [".world", ".sdf"]:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin world artifact path must end with .world.sdf"
        )
    absolute_path = Path(repo_root).resolve() / normalized
    if not absolute_path.exists():
        raise DigitalTwinMissionEnvironmentError(
            f"Digital Twin world artifact file does not exist: {normalized}"
        )
    actual_sha256 = sha256(absolute_path.read_bytes()).hexdigest()
    if actual_sha256 != artifact.world_file_sha256:
        raise DigitalTwinMissionEnvironmentError(
            "Digital Twin world artifact file hash mismatch"
        )
    return normalized


def build_digital_twin_gazebo_command(
    *,
    world_path: Path,
    repo_root: str | Path = ".",
    container_name: str | None = None,
) -> tuple[str, ...]:
    command_template = os.getenv("DIGITAL_TWIN_GZ_SIM_COMMAND", "").strip()
    if command_template:
        parts = tuple(shlex.split(command_template))
        return tuple(
            part.format(world_path=world_path.as_posix(), repo_root=str(repo_root))
            for part in parts
        )
    if shutil.which("gz"):
        return ("gz", "sim", "-s", "-r", world_path.as_posix())
    if shutil.which("docker"):
        image = os.getenv(
            "DIGITAL_TWIN_GAZEBO_DOCKER_IMAGE",
            DEFAULT_GAZEBO_DOCKER_IMAGE,
        )
        name = container_name or f"digital-twin-gz-{world_path.stem[:12]}"
        return (
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "-v",
            f"{Path(repo_root).resolve()}:/workspace",
            "-w",
            "/workspace",
            image,
            "gz",
            "sim",
            "-s",
            "-r",
            world_path.as_posix(),
        )
    raise DigitalTwinMissionEnvironmentError(
        "Digital Twin SITL process runner requires gz or docker"
    )


def run_digital_twin_sitl_process(
    *,
    gazebo_world_artifact: GazeboWorldArtifact | Mapping[str, Any],
    repo_root: str | Path = ".",
    output_root: str | Path = DIGITAL_TWIN_SITL_RUN_ROOT,
    startup_window_seconds: float = 8.0,
    cleanup_timeout_seconds: float = 5.0,
    now: datetime | None = None,
) -> DigitalTwinSITLProcessRun:
    artifact = (
        gazebo_world_artifact
        if isinstance(gazebo_world_artifact, GazeboWorldArtifact)
        else GazeboWorldArtifact.model_validate(gazebo_world_artifact)
    )
    started_at = _utc(now)
    world_path = resolve_digital_twin_world_path(artifact, repo_root=repo_root)
    world_ref = gazebo_world_artifact_ref(artifact)
    process_run_seed = {
        "schema_version": DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION,
        "gazebo_world_artifact_ref": world_ref,
        "world_file_sha256": artifact.world_file_sha256,
        "started_at": started_at.isoformat(),
    }
    process_run_id = "digital_twin_sitl_process_run_" + _content_hash(
        process_run_seed
    )[:12]
    run_dir = Path(output_root) / process_run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "gazebo.stdout.log"
    stderr_path = run_dir / "gazebo.stderr.log"
    container_name = _docker_container_name(process_run_id)
    command = build_digital_twin_gazebo_command(
        world_path=world_path,
        repo_root=repo_root,
        container_name=container_name,
    )

    exit_status: str = "startup_failed"
    exit_code: int | None = None
    process_pid: int | None = None
    blocked_reasons: tuple[str, ...] = ()
    startup_error_observed = False
    stopped_at: datetime | None = None
    with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_file:
        try:
            process = subprocess.Popen(  # noqa: S603 - command is constructed from allowlisted runner inputs.
                command,
                cwd=Path(repo_root).resolve(),
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
            )
            process_pid = int(process.pid)
            deadline = time.monotonic() + startup_window_seconds
            while time.monotonic() < deadline:
                exit_code = process.poll()
                if exit_code is not None:
                    exit_status = "exited"
                    break
                time.sleep(0.2)
            if process.poll() is None:
                process.terminate()
                try:
                    exit_code = process.wait(timeout=cleanup_timeout_seconds)
                    exit_status = "terminated_after_startup_window"
                except subprocess.TimeoutExpired:
                    process.kill()
                    exit_code = process.wait(timeout=cleanup_timeout_seconds)
                    exit_status = "killed_after_cleanup_timeout"
            else:
                exit_code = process.returncode
        except Exception as exc:
            blocked_reasons = (f"process_startup_failed:{type(exc).__name__}",)
            exit_status = "startup_failed"
            stderr_file.write(f"\nDigital Twin SITL process startup failed: {exc}\n")
        finally:
            stopped_at = _utc()
            if command[:3] == ("docker", "run", "--rm"):
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    cwd=Path(repo_root).resolve(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    text=True,
                    timeout=15,
                )

    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    # v1 heuristic: Gazebo can exit successfully while logging fatal DEM/SDF
    # loader errors. Later E2E smokes should replace this with a health signal.
    startup_error_observed = any(
        marker in stderr_text
        for marker in (
            "[Err]",
            "Unable to open DEM file",
            "Failed to load",
            "Error Code",
        )
    )
    if startup_error_observed:
        exit_status = "startup_failed"
        blocked_reasons = (*blocked_reasons, "gazebo_startup_error_observed")

    process_launch_attempted = process_pid is not None
    gazebo_execution_invoked = process_launch_attempted
    payload = {
        "schema_version": DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION,
        "gazebo_world_artifact_ref": world_ref,
        "world_file_path_or_artifact_uri": artifact.world_file_path_or_artifact_uri,
        "world_file_sha256": artifact.world_file_sha256,
        "process_launch_attempted": process_launch_attempted,
        "gazebo_execution_invoked": gazebo_execution_invoked,
        "px4_process_invoked": False,
        "process_pids": (process_pid,) if process_pid is not None else (),
        "command": command,
        "world_artifact_load_mode": "direct_world_artifact_load",
        "px4_loaded_world_file_path": artifact.world_file_path_or_artifact_uri,
        "started_at": started_at.isoformat(),
        "stopped_at": stopped_at.isoformat() if stopped_at else None,
        "exit_status": exit_status,
        "exit_code": exit_code,
        "stdout_ref": str(stdout_path),
        "stderr_ref": str(stderr_path),
        "startup_error_observed": startup_error_observed,
        "simulation_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "approval_free_stronger_execution_allowed": False,
        "blocked_reasons": blocked_reasons,
    }
    digest = _content_hash(payload)
    return DigitalTwinSITLProcessRun(
        process_run_id=process_run_id,
        gazebo_world_artifact_ref=world_ref,
        world_file_path_or_artifact_uri=artifact.world_file_path_or_artifact_uri,
        world_file_sha256=artifact.world_file_sha256,
        process_launch_attempted=process_launch_attempted,
        gazebo_execution_invoked=gazebo_execution_invoked,
        px4_process_invoked=False,
        process_pids=(process_pid,) if process_pid is not None else (),
        command=command,
        world_artifact_load_mode="direct_world_artifact_load",
        px4_loaded_world_file_path=artifact.world_file_path_or_artifact_uri,
        started_at=started_at,
        stopped_at=stopped_at,
        exit_status=exit_status,  # type: ignore[arg-type]
        exit_code=exit_code,
        stdout_ref=str(stdout_path),
        stderr_ref=str(stderr_path),
        startup_error_observed=startup_error_observed,
        run_hash=digest,
        sha256=digest,
        blocked_reasons=blocked_reasons,
    )


def build_digital_twin_sitl_process_run_from_observed_container(
    *,
    gazebo_world_artifact: GazeboWorldArtifact | Mapping[str, Any],
    command: Sequence[str],
    process_pids: Sequence[int],
    stdout_ref: str,
    stderr_ref: str,
    started_at: datetime,
    stopped_at: datetime | None,
    exit_status: Literal[
        "not_attempted",
        "startup_failed",
        "exited",
        "terminated_after_startup_window",
        "killed_after_cleanup_timeout",
    ],
    exit_code: int | None,
    startup_error_observed: bool,
    px4_process_invoked: bool,
    world_artifact_load_mode: Literal[
        "direct_world_artifact_load",
        "terrain_injection_into_default_world",
    ] = "direct_world_artifact_load",
    px4_loaded_world_file_path: str = "",
    blocked_reasons: Sequence[str] = (),
    repo_root: str | Path = ".",
) -> DigitalTwinSITLProcessRun:
    artifact = (
        gazebo_world_artifact
        if isinstance(gazebo_world_artifact, GazeboWorldArtifact)
        else GazeboWorldArtifact.model_validate(gazebo_world_artifact)
    )
    world_path = resolve_digital_twin_world_path(artifact, repo_root=repo_root)
    world_ref = gazebo_world_artifact_ref(artifact)
    run_seed = {
        "schema_version": DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION,
        "gazebo_world_artifact_ref": world_ref,
        "world_file_sha256": artifact.world_file_sha256,
        "started_at": _utc(started_at).isoformat(),
        "command": tuple(command),
    }
    process_run_id = "digital_twin_sitl_process_run_" + _content_hash(run_seed)[:12]
    reasons = tuple(str(reason) for reason in blocked_reasons)
    if startup_error_observed and "gazebo_startup_error_observed" not in set(reasons):
        reasons = (*reasons, "gazebo_startup_error_observed")
    payload = {
        "schema_version": DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION,
        "gazebo_world_artifact_ref": world_ref,
        "world_file_path_or_artifact_uri": artifact.world_file_path_or_artifact_uri,
        "world_file_sha256": artifact.world_file_sha256,
        "process_launch_attempted": bool(process_pids),
        "gazebo_execution_invoked": bool(process_pids),
        "px4_process_invoked": px4_process_invoked,
        "process_pids": tuple(int(pid) for pid in process_pids),
        "command": tuple(str(part) for part in command),
        "world_artifact_load_mode": world_artifact_load_mode,
        "px4_loaded_world_file_path": px4_loaded_world_file_path,
        "started_at": _utc(started_at).isoformat(),
        "stopped_at": _utc(stopped_at).isoformat() if stopped_at else None,
        "exit_status": exit_status,
        "exit_code": exit_code,
        "stdout_ref": stdout_ref,
        "stderr_ref": stderr_ref,
        "startup_error_observed": startup_error_observed,
        "simulation_only": True,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "approval_free_stronger_execution_allowed": False,
        "blocked_reasons": reasons,
    }
    digest = _content_hash(payload)
    return DigitalTwinSITLProcessRun(
        process_run_id=process_run_id,
        gazebo_world_artifact_ref=world_ref,
        world_file_path_or_artifact_uri=world_path.as_posix(),
        world_file_sha256=artifact.world_file_sha256,
        process_launch_attempted=bool(process_pids),
        gazebo_execution_invoked=bool(process_pids),
        px4_process_invoked=px4_process_invoked,
        process_pids=tuple(int(pid) for pid in process_pids),
        command=tuple(str(part) for part in command),
        world_artifact_load_mode=world_artifact_load_mode,
        px4_loaded_world_file_path=px4_loaded_world_file_path,
        started_at=_utc(started_at),
        stopped_at=_utc(stopped_at) if stopped_at else None,
        exit_status=exit_status,
        exit_code=exit_code,
        stdout_ref=stdout_ref,
        stderr_ref=stderr_ref,
        startup_error_observed=startup_error_observed,
        run_hash=digest,
        sha256=digest,
        blocked_reasons=reasons,
    )


__all__ = [
    "DEFAULT_GAZEBO_DOCKER_IMAGE",
    "DIGITAL_TWIN_SITL_PROCESS_RUN_SCHEMA_VERSION",
    "DigitalTwinSITLProcessRun",
    "build_digital_twin_sitl_process_run_from_observed_container",
    "build_digital_twin_gazebo_command",
    "digital_twin_sitl_process_run_ref",
    "resolve_digital_twin_world_path",
    "run_digital_twin_sitl_process",
]
