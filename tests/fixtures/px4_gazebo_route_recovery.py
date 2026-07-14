from dataclasses import dataclass
import socket
import threading
from typing import Any

from src.runtime.px4_gazebo_coupled_delivery import (
    build_px4_gazebo_coupled_command_approval,
)
from src.runtime.px4_gazebo_route_dispatcher import (
    build_px4_gazebo_route_command_allowlist,
    build_px4_gazebo_route_progress_evidence,
    run_px4_gazebo_route_command_dispatch,
)
from src.runtime.px4_gazebo_route_plan import (
    build_px4_gazebo_pickup_dropoff_route_plan,
)
from src.runtime.px4_gazebo_route_recovery import PX4GazeboRouteGoldenCorpusCase
from tests.fixtures.delivery_artifact_chain import NOW


@dataclass(frozen=True)
class RouteBundle:
    route: Any
    dispatch: Any
    progress: Any
    datagrams_received: int


class FakeRoutePX4Endpoint:
    """Loopback-only UDP sink for exercising bounded dispatch serialization."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port: int | None = None

    def __enter__(self) -> "FakeRoutePX4Endpoint":
        self._thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("fake route endpoint did not start")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"x", ("127.0.0.1", self.port or 9))
        self._thread.join(timeout=2)

    def _run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.settimeout(0.2)
            self.port = int(sock.getsockname()[1])
            self._ready.set()
            while not self._stop.is_set():
                try:
                    data, _addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                if data != b"x":
                    self.received.append(data)


def build_route_bundle() -> RouteBundle:
    route = build_px4_gazebo_pickup_dropoff_route_plan(
        pickup_pad_ref="gazebo_pad:pickup",
        dropoff_pad_ref="gazebo_pad:dropoff",
        route_waypoint_refs=["gazebo_waypoint:mid"],
        geofence_polygon=[(-2.0, -2.0), (8.0, -2.0), (8.0, 8.0), (-2.0, 8.0)],
        altitude_min_m=1.0,
        altitude_max_m=4.0,
        min_battery_margin_pct=25.0,
        now=NOW,
    )
    approval = build_px4_gazebo_coupled_command_approval(
        operator_approval_performed=True,
        now=NOW,
    )
    allowlist = build_px4_gazebo_route_command_allowlist(
        route_plan=route,
        approval=approval,
        now=NOW,
    )
    with FakeRoutePX4Endpoint() as endpoint:
        if endpoint.port is None:
            raise RuntimeError("fake route endpoint did not publish a port")
        dispatch = run_px4_gazebo_route_command_dispatch(
            route_plan=route,
            route_allowlist=allowlist,
            approval=approval,
            endpoint_port=endpoint.port,
            live_mavlink_opt_in=True,
            now=NOW,
        )
    progress = build_px4_gazebo_route_progress_evidence(
        route_plan=route,
        route_dispatch_result=dispatch,
        pickup_pose_xy_m=(0.0, 0.0),
        observed_pose_xy_m=(7.25, 4.0),
        now=NOW,
    )
    return RouteBundle(
        route=route,
        dispatch=dispatch,
        progress=progress,
        datagrams_received=len(endpoint.received),
    )


def route_recovery_extra_cases() -> tuple[PX4GazeboRouteGoldenCorpusCase, ...]:
    case_data = (
        (
            "blocked:mavlink_timeout",
            "blocked",
            "px4_gazebo_route_recovery_diagnostics.v1",
            ("mavlink_timeout",),
            None,
            None,
        ),
        (
            "blocked:command_rejected",
            "blocked",
            "px4_gazebo_route_recovery_diagnostics.v1",
            ("command_rejected",),
            None,
            None,
        ),
        (
            "blocked:wrong_target",
            "blocked",
            "px4_gazebo_route_recovery_diagnostics.v1",
            ("wrong_target",),
            None,
            None,
        ),
        (
            "blocked:route_geofence_violation",
            "blocked",
            "px4_gazebo_route_delivery_completion_gate.v1",
            ("route_geofence_violation",),
            None,
            None,
        ),
        (
            "blocked:route_pose_missing",
            "blocked",
            "px4_gazebo_route_delivery_completion_gate.v1",
            ("route_pose_missing",),
            None,
            None,
        ),
        (
            "blocked:missing_px4_telemetry",
            "blocked",
            "px4_gazebo_route_delivery_completion_gate.v1",
            ("missing_px4_telemetry_correlated",),
            None,
            None,
        ),
        (
            "rejection:command_like_metadata",
            "blocked",
            "px4_gazebo_route_recovery_diagnostics.v1",
            ("command_like_metadata_rejected",),
            None,
            None,
        ),
        (
            "rejection:hardware_target_override",
            "blocked",
            "px4_gazebo_route_recovery_diagnostics.v1",
            ("hardware_target_override_rejected",),
            None,
            None,
        ),
        (
            "recovery:state_observed_after_dispatch_timeout",
            "completed",
            "px4_gazebo_route_recovery_completion.v1",
            (),
            "state_observed_after_dispatch_timeout",
            None,
        ),
        (
            "recovery:hold_state_observed_after_dispatch_timeout",
            "completed",
            "px4_gazebo_route_recovery_completion.v1",
            (),
            "state_observed_after_dispatch_timeout",
            "hold",
        ),
        (
            "recovery:rtl_ack_observed_and_state_observed",
            "completed",
            "px4_gazebo_route_recovery_completion.v1",
            (),
            "ack_observed_and_state_observed",
            "return_to_launch",
        ),
        (
            "recovery:state_not_observed_after_dispatch_timeout",
            "blocked",
            "px4_gazebo_route_recovery_completion.v1",
            ("emergency_recovery_unconfirmed",),
            "state_not_observed_after_dispatch_timeout",
            None,
        ),
        (
            "recovery:dispatch_blocked_before_send",
            "blocked",
            "px4_gazebo_route_recovery_completion.v1",
            ("emergency_recovery_dispatch_blocked",),
            "dispatch_blocked_before_send",
            None,
        ),
    )
    return tuple(
        PX4GazeboRouteGoldenCorpusCase(
            case_id=case_id,
            expected_terminal_status=terminal_status,
            required_artifact_schema_versions=(schema_version,),
            expected_blocked_reasons=blocked_reasons,
            expected_recovery_completion_basis=completion_basis,
            expected_recovery_action=recovery_action,
        )
        for (
            case_id,
            terminal_status,
            schema_version,
            blocked_reasons,
            completion_basis,
            recovery_action,
        ) in case_data
    )
