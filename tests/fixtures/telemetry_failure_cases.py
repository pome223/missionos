from dataclasses import dataclass
from datetime import datetime, timezone
import json

from src.runtime.gazebo_log_collector import GAZEBO_TELEMETRY_STARTUP_MARKER
from src.runtime.gz_sim_log_collector import GZ_SIM_DELIVERY_WORLD_SDF_PATH
from src.runtime.px4_gazebo_log_collector import PX4_GAZEBO_TELEMETRY_LOG_PREFIX


CAPTURED_AT = datetime(2026, 4, 30, 17, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class TelemetryFailureCase:
    case_id: str
    log_text: str


def _command_like_telemetry_payload() -> str:
    return PX4_GAZEBO_TELEMETRY_LOG_PREFIX + json.dumps(
        {
            "sample_id": "command-like-fixture",
            "source": {
                "source_kind": "px4_gazebo_compatible_log_source",
                "source_id": "invalid-fixture-source",
                "vehicle_id": "invalid-fixture-vehicle",
            },
            "captured_at": CAPTURED_AT.isoformat(),
            "telemetry": {"altitude_m": 1.0},
            "metadata": {"nested": [{"RosTopic": "/cmd_vel"}]},
        },
        sort_keys=True,
    )


PX4_SITL_FAILURE_CASES = (
    TelemetryFailureCase("no_output", ""),
    TelemetryFailureCase("prompt_only", "pxh>\npxh>\npxh>"),
    TelemetryFailureCase(
        "rootfs_failure",
        "ERROR [px4] Error creating directory: "
        "/root/.local/share/px4/rootfs (Read-only file system)",
    ),
    TelemetryFailureCase(
        "partial_startup",
        "\n".join(
            (
                "INFO  [px4] startup script: /bin/sh etc/init.d-posix/rcS 0",
                "INFO  [init] found model autostart file as SYS_AUTOSTART=10040",
                "INFO  [init] SIH simulator",
            )
        ),
    ),
    TelemetryFailureCase("unknown_model", "ERROR [init] Unknown model sihsim_missing"),
)

PX4_GAZEBO_LOG_FAILURE_CASES = (
    TelemetryFailureCase("no_output", "container | started without telemetry"),
    TelemetryFailureCase(
        "malformed_log", PX4_GAZEBO_TELEMETRY_LOG_PREFIX + "{not-json"
    ),
    TelemetryFailureCase("command_like_payload", _command_like_telemetry_payload()),
)

GAZEBO_LOG_FAILURE_CASES = (
    TelemetryFailureCase("no_output", ""),
    TelemetryFailureCase("missing_startup_marker", "PX4_GAZEBO_TELEMETRY {}"),
    TelemetryFailureCase("startup_only", GAZEBO_TELEMETRY_STARTUP_MARKER),
    TelemetryFailureCase(
        "malformed_log",
        "\n".join(
            (
                GAZEBO_TELEMETRY_STARTUP_MARKER,
                PX4_GAZEBO_TELEMETRY_LOG_PREFIX + "{not-json",
            )
        ),
    ),
    TelemetryFailureCase(
        "command_like_payload",
        "\n".join(
            (
                GAZEBO_TELEMETRY_STARTUP_MARKER,
                _command_like_telemetry_payload(),
            )
        ),
    ),
)

GZ_SIM_FAILURE_CASES = (
    TelemetryFailureCase("no_output", ""),
    TelemetryFailureCase(
        "missing_server_marker",
        "\n".join(
            ("[Msg] Loading SDF world file[/tmp/empty.sdf].", "[Msg] Loaded level [default]")
        ),
    ),
    TelemetryFailureCase(
        "missing_world_marker",
        "\n".join(
            ("[Msg] Gazebo Sim Server v8.11.0", "[Msg] Loading SDF world file[/tmp/empty.sdf].")
        ),
    ),
    TelemetryFailureCase("startup_only", "[Msg] Gazebo Sim Server v8.11.0"),
    TelemetryFailureCase(
        "world_load_failure",
        "\n".join(
            (
                "[Msg] Gazebo Sim Server v8.11.0",
                "[Err] Unable to find or load SDF world file[/tmp/missing.sdf].",
            )
        ),
    ),
    TelemetryFailureCase(
        "command_like_payload",
        "\n".join(
            (
                "[Msg] Gazebo Sim Server v8.11.0",
                "[Msg] Loading SDF world file[/tmp/empty.sdf].",
                "[Msg] Loaded level [default]",
                '{"metadata": {"nested": [{"RosTopic": "/cmd_vel"}]}}',
            )
        ),
    ),
)

DELIVERY_WORLD_LOG = "\n".join(
    (
        "[Msg] Gazebo Sim Server v8.11.0",
        f"[Msg] Loading SDF world file[{GZ_SIM_DELIVERY_WORLD_SDF_PATH}].",
        "[Msg] Loaded level [default]",
        "[Msg] World [delivery_minimal] initialized.",
    )
)

GZ_SIM_DELIVERY_FAILURE_CASES = (
    TelemetryFailureCase("no_output", ""),
    TelemetryFailureCase(
        "missing_server_marker",
        "\n".join(
            (
                f"[Msg] Loading SDF world file[{GZ_SIM_DELIVERY_WORLD_SDF_PATH}].",
                "[Msg] Loaded level [default]",
            )
        ),
    ),
    TelemetryFailureCase(
        "missing_world_marker",
        "\n".join(
            (
                "[Msg] Gazebo Sim Server v8.11.0",
                f"[Msg] Loading SDF world file[{GZ_SIM_DELIVERY_WORLD_SDF_PATH}].",
            )
        ),
    ),
    TelemetryFailureCase("startup_only", "[Msg] Gazebo Sim Server v8.11.0"),
    TelemetryFailureCase(
        "world_load_failure",
        "\n".join(
            (
                "[Msg] Gazebo Sim Server v8.11.0",
                "[Err] Unable to find or load SDF world file[/worlds/missing.sdf].",
            )
        ),
    ),
    TelemetryFailureCase(
        "delivery_world_mismatch",
        "\n".join(
            (
                "[Msg] Gazebo Sim Server v8.11.0",
                "[Msg] Loading SDF world file[/tmp/empty.sdf].",
                "[Msg] Loaded level [default]",
                "[Msg] World [empty] initialized.",
            )
        ),
    ),
    TelemetryFailureCase(
        "command_like_payload",
        DELIVERY_WORLD_LOG
        + '\n{"metadata": {"nested": [{"RosTopic": "/cmd_vel"}]}}',
    ),
)
