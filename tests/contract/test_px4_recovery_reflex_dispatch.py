"""Contract tests for reflex-authority RTL dispatch (issue #31).

This dispatch path is deliberately separate from every operator-approval-
gated live MAVLink pathway in this codebase: it never claims
operator_approval_performed=True, and it only ever fires for a genuinely
exhausted PX4 recovery reflex, targeting RETURN_TO_LAUNCH exclusively.
"""

from __future__ import annotations

from datetime import datetime, timezone
import socket
import threading

from src.runtime.px4_mavlink_ack_state import MAV_RESULT_ACCEPTED, encode_mavlink2_command_ack
from src.runtime.px4_gazebo_emergency_dispatcher import MAV_CMD_NAV_RETURN_TO_LAUNCH
from src.runtime.px4_real_mavlink_transport import decode_mavlink2_frame
from src.runtime.px4_recovery_reflex import build_px4_recovery_reflex
from src.runtime.px4_recovery_reflex_dispatch import (
    PX4_RECOVERY_REFLEX_DISPATCH_SCHEMA_VERSION,
    PX4RecoveryReflexDispatchStatus,
    dispatch_px4_recovery_reflex_rtl,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _exhausted_reflex():
    return build_px4_recovery_reflex(
        trigger="battery_below_reserve_landing_threshold",
        battery_percent=25.0,
        reserve_landing_percent=25.0,
        now=NOW,
    )


def _loitering_reflex():
    return build_px4_recovery_reflex(
        trigger="battery_below_return_to_home_threshold",
        battery_percent=30.0,
        reserve_landing_percent=25.0,
        now=NOW,
    )


def test_blocked_when_reflex_budget_not_exhausted() -> None:
    result = dispatch_px4_recovery_reflex_rtl(
        reflex=_loitering_reflex(),
        live_mavlink_opt_in=True,
        now=NOW,
    )
    assert result.dispatch_status == PX4RecoveryReflexDispatchStatus.BLOCKED
    assert "reflex_action_not_return_to_home_recommended" in result.blocked_reasons
    assert "reflex_budget_not_exhausted" in result.blocked_reasons
    assert "reflex_still_in_deliberation" in result.blocked_reasons
    assert result.reflex_authority_performed is False
    assert result.mavlink_frame_sent is False


def test_blocked_when_live_mavlink_opt_in_missing() -> None:
    result = dispatch_px4_recovery_reflex_rtl(
        reflex=_exhausted_reflex(),
        live_mavlink_opt_in=False,
        now=NOW,
    )
    assert result.dispatch_status == PX4RecoveryReflexDispatchStatus.BLOCKED
    assert result.blocked_reasons == ("live_mavlink_opt_in_not_enabled",)


def test_blocked_when_endpoint_is_not_loopback() -> None:
    result = dispatch_px4_recovery_reflex_rtl(
        reflex=_exhausted_reflex(),
        live_mavlink_opt_in=True,
        endpoint_host="10.0.0.5",
        now=NOW,
    )
    assert "reflex_dispatch_must_target_loopback" in result.blocked_reasons


def test_result_never_claims_operator_approval() -> None:
    result = dispatch_px4_recovery_reflex_rtl(
        reflex=_loitering_reflex(),
        live_mavlink_opt_in=True,
        now=NOW,
    )
    assert result.schema_version == PX4_RECOVERY_REFLEX_DISPATCH_SCHEMA_VERSION
    assert result.operator_approval_performed is False
    assert result.physical_execution_invoked is False
    assert result.hardware_target_allowed is False
    assert "operator" in result.claim_boundary.lower()


def _run_fake_px4_endpoint(sock: socket.socket, ready: threading.Event) -> None:
    ready.set()
    remote = None
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError:
            return
        remote = addr
        try:
            decoded = decode_mavlink2_frame(data)
        except Exception:
            continue
        if decoded["msg_id"] == 76:  # COMMAND_LONG
            ack = encode_mavlink2_command_ack(
                command_id=MAV_CMD_NAV_RETURN_TO_LAUNCH,
                result_code=MAV_RESULT_ACCEPTED,
            )
            sock.sendto(ack, remote)
            return


def test_dispatches_bounded_rtl_and_observes_accepted_ack() -> None:
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.settimeout(5.0)
    _host, server_port = server_sock.getsockname()

    ready = threading.Event()
    thread = threading.Thread(
        target=_run_fake_px4_endpoint, args=(server_sock, ready), daemon=True
    )
    thread.start()
    ready.wait(timeout=2.0)
    try:
        result = dispatch_px4_recovery_reflex_rtl(
            reflex=_exhausted_reflex(),
            live_mavlink_opt_in=True,
            endpoint_port=server_port,
            ack_timeout_seconds=3.0,
            heartbeat_warmup_frames=1,
            heartbeat_warmup_interval_seconds=0.0,
            now=NOW,
        )
    finally:
        thread.join(timeout=2.0)
        server_sock.close()

    assert result.dispatch_status == PX4RecoveryReflexDispatchStatus.DISPATCHED
    assert result.reflex_authority_performed is True
    assert result.operator_approval_performed is False
    assert result.command_id == MAV_CMD_NAV_RETURN_TO_LAUNCH
    assert result.command_name == "MAV_CMD_NAV_RETURN_TO_LAUNCH"
    assert result.mavlink_socket_opened is True
    assert result.mavlink_frame_sent is True
    assert result.command_ack_observed is True
    assert result.command_ack_accepted is True
    assert result.blocked_reasons == ()
    assert result.reflex_trigger == "battery_below_reserve_landing_threshold"


def test_dispatch_times_out_gracefully_with_no_responder() -> None:
    unused_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    unused_sock.bind(("127.0.0.1", 0))
    _host, dead_port = unused_sock.getsockname()
    unused_sock.close()

    result = dispatch_px4_recovery_reflex_rtl(
        reflex=_exhausted_reflex(),
        live_mavlink_opt_in=True,
        endpoint_port=dead_port,
        ack_timeout_seconds=0.5,
        heartbeat_warmup_frames=1,
        heartbeat_warmup_interval_seconds=0.0,
        now=NOW,
    )

    assert result.dispatch_status == PX4RecoveryReflexDispatchStatus.DISPATCHED
    assert result.mavlink_frame_sent is True
    assert result.command_ack_observed is False
    assert result.command_ack_accepted is False


def test_default_dispatcher_factory_is_env_gated(monkeypatch) -> None:
    from src.runtime.px4_recovery_reflex_dispatch import (
        PX4_REFLEX_RTL_ENABLED_ENV,
        default_px4_reflex_dispatcher_from_env,
    )

    monkeypatch.delenv(PX4_REFLEX_RTL_ENABLED_ENV, raising=False)
    assert default_px4_reflex_dispatcher_from_env() is None

    monkeypatch.setenv(PX4_REFLEX_RTL_ENABLED_ENV, "1")
    dispatcher = default_px4_reflex_dispatcher_from_env()
    assert dispatcher is not None
    # A non-exhausted reflex is blocked before any socket is opened.
    blocked = dispatcher(_loitering_reflex().model_dump(mode="json"))
    assert blocked["dispatch_status"] == "blocked"
    assert blocked["mavlink_frame_sent"] is False


def test_battery_watch_within_budget_is_record_only() -> None:
    from src.runtime.px4_recovery_reflex_dispatch import (
        watch_px4_recovery_reflex_from_battery,
    )

    watch = watch_px4_recovery_reflex_from_battery(
        battery_remaining_percent=80.0,
        reserve_landing_percent=25.0,
        dispatcher=lambda reflex: {"dispatch_status": "dispatched"},
    )
    assert watch["watch_status"] == "within_budget"
    assert watch["dispatch"] == {}
    assert watch["reflex"]["budget_exhausted"] is False
    assert watch["dispatch_authority_created"] is False


def test_battery_watch_exhausted_without_dispatcher_records_only() -> None:
    from src.runtime.px4_recovery_reflex_dispatch import (
        watch_px4_recovery_reflex_from_battery,
    )

    watch = watch_px4_recovery_reflex_from_battery(
        battery_remaining_percent=20.0,
        reserve_landing_percent=25.0,
        dispatcher=None,
    )
    assert watch["watch_status"] == "exhausted_record_only"
    assert watch["dispatch"] == {}
    assert watch["reflex"]["budget_exhausted"] is True


def test_battery_watch_exhausted_dispatches_once() -> None:
    from src.runtime.px4_recovery_reflex_dispatch import (
        watch_px4_recovery_reflex_from_battery,
    )

    calls: list = []

    def _fake_dispatcher(reflex: dict) -> dict:
        calls.append(reflex)
        return {"dispatch_status": "dispatched", "reflex_authority_performed": True}

    watch = watch_px4_recovery_reflex_from_battery(
        battery_remaining_percent=20.0,
        reserve_landing_percent=25.0,
        dispatcher=_fake_dispatcher,
    )
    assert watch["watch_status"] == "dispatched"
    assert watch["dispatch"]["reflex_authority_performed"] is True
    assert len(calls) == 1
    assert calls[0]["budget_exhausted"] is True

    repeat = watch_px4_recovery_reflex_from_battery(
        battery_remaining_percent=18.0,
        reserve_landing_percent=25.0,
        dispatcher=_fake_dispatcher,
        already_dispatched=True,
    )
    assert repeat["watch_status"] == "already_dispatched"
    assert len(calls) == 1


def test_live_flight_watch_attaches_and_dedupes_dispatch(tmp_path) -> None:
    """The live SITL polling loop's watcher: dispatch fires exactly once."""

    from src.runtime.px4_gazebo_mission_designer_sitl_live_flight_run import (
        _attach_px4_reflex_watch,
    )
    from src.runtime.task_store import TaskStore

    store = TaskStore(f"{tmp_path}/tasks.db")
    task = store.create(
        kind="mission_designer_live_flight",
        title="px4 reflex watch test",
        status="running",
        artifacts={},
    )
    calls: list = []

    def _factory():
        def _dispatch(reflex: dict) -> dict:
            calls.append(reflex)
            return {"dispatch_status": "dispatched"}

        return _dispatch

    first = _attach_px4_reflex_watch(
        task_id=task["task_id"],
        battery_remaining_percent=20.0,
        store=store,
        dispatcher_factory=_factory,
    )
    assert first["watch_status"] == "dispatched"
    assert len(calls) == 1

    second = _attach_px4_reflex_watch(
        task_id=task["task_id"],
        battery_remaining_percent=15.0,
        store=store,
        dispatcher_factory=_factory,
    )
    assert second["watch_status"] == "dispatched"
    assert second["latest_reflex"]["battery_percent"] == 15.0
    assert len(calls) == 1

    stored = store.get(task["task_id"])
    assert stored is not None
    assert (
        stored["artifacts"]["px4_recovery_reflex_watch"]["watch_status"]
        == "dispatched"
    )
