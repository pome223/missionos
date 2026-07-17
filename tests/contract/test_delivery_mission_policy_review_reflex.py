"""Contract tests wiring the PX4 reflex into delivery_mission_policy_review.

The policy review is the earliest point in the delivery recovery chain that
has both a battery_percent reading and the contract's reserve_landing_percent
policy, so it is where the budgeted-loiter reflex is attached (issue #31).
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.runtime.delivery_mission_contract import build_delivery_mission_contract
from src.runtime.delivery_mission_policy_review import (
    DELIVERY_POLICY_BUCKET_BATTERY_ABORT_RECOMMENDED,
    DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED,
    build_delivery_mission_policy_review,
)
from src.runtime.px4_gazebo_telemetry import sanitize_px4_gazebo_telemetry_sample

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _contract():
    return build_delivery_mission_contract(
        mission_id="policy-review-reflex-001",
        pickup_location={
            "location_id": "pickup-pad-a",
            "latitude": 35.681236,
            "longitude": 139.767125,
        },
        dropoff_location={
            "location_id": "dropoff-pad-b",
            "latitude": 35.689487,
            "longitude": 139.691706,
        },
        delivery_window={
            "earliest_pickup_at": "2026-07-15T12:00:00Z",
            "latest_dropoff_at": "2026-07-15T12:30:00Z",
        },
        package_constraints={"package_id": "pkg-reflex", "max_weight_kg": 1.0},
        geofence_constraints={"allowed_regions": ["sim-delivery-corridor"]},
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
            "required_measurements": ["position", "battery_percent"],
            "max_freshness_seconds": 2.0,
        },
        now=NOW,
    )


def _telemetry(battery_percent: float):
    return sanitize_px4_gazebo_telemetry_sample(
        {
            "sample_id": "policy-review-reflex",
            "source": {
                "source_kind": "gz_sim_harmonic_stdout_log",
                "source_id": "gz-sim-reflex",
                "vehicle_id": "vehicle-reflex",
            },
            "captured_at": "2026-07-15T12:00:00Z",
            "telemetry": {
                "position": "35.681236,139.767125,16.0",
                "battery_percent": battery_percent,
            },
        }
    )


def test_return_home_threshold_attaches_loiter_budget_reflex() -> None:
    review = build_delivery_mission_policy_review(
        delivery_mission_contract=_contract(),
        sanitized_telemetry=_telemetry(30.0),
        now=NOW,
    )
    assert (
        DELIVERY_POLICY_BUCKET_BATTERY_RETURN_HOME_RECOMMENDED
        in review.warning_reasons
    )
    reflex = review.px4_recovery_reflex
    assert reflex["trigger"] == "battery_below_return_to_home_threshold"
    assert reflex["battery_percent"] == 30.0
    assert reflex["reserve_landing_percent"] == 25.0
    assert reflex["available_pct_before_reserve"] == 5.0
    assert reflex["reflex_action"] == "loiter"
    assert reflex["budget_exhausted"] is False
    assert reflex["dispatch_authority_created"] is False
    assert reflex["physical_execution_invoked"] is False


def test_reserve_landing_threshold_exhausts_budget_and_forces_rth() -> None:
    review = build_delivery_mission_policy_review(
        delivery_mission_contract=_contract(),
        sanitized_telemetry=_telemetry(20.0),
        now=NOW,
    )
    assert (
        DELIVERY_POLICY_BUCKET_BATTERY_ABORT_RECOMMENDED in review.blocked_reasons
    )
    reflex = review.px4_recovery_reflex
    assert reflex["trigger"] == "battery_below_reserve_landing_threshold"
    assert reflex["available_pct_before_reserve"] == 0.0
    assert reflex["reflex_action"] == "return_to_home_recommended"
    assert reflex["budget_exhausted"] is True
    assert reflex["entered_deliberation"] is False


def test_healthy_battery_records_no_reflex() -> None:
    review = build_delivery_mission_policy_review(
        delivery_mission_contract=_contract(),
        sanitized_telemetry=_telemetry(90.0),
        now=NOW,
    )
    assert review.px4_recovery_reflex == {}


def test_reserve_landing_threshold_invokes_dispatcher_with_reflex() -> None:
    received: dict = {}

    def _fake_dispatcher(reflex: dict) -> dict:
        received.update(reflex)
        return {
            "dispatch_status": "dispatched",
            "reflex_authority_performed": True,
            "operator_approval_performed": False,
            "command_ack_accepted": True,
        }

    review = build_delivery_mission_policy_review(
        delivery_mission_contract=_contract(),
        sanitized_telemetry=_telemetry(20.0),
        now=NOW,
        px4_reflex_dispatcher=_fake_dispatcher,
    )
    assert received["trigger"] == "battery_below_reserve_landing_threshold"
    assert received["budget_exhausted"] is True
    dispatch = review.px4_recovery_reflex["dispatch"]
    assert dispatch["dispatch_status"] == "dispatched"
    assert dispatch["reflex_authority_performed"] is True
    assert dispatch["operator_approval_performed"] is False


def test_return_home_threshold_does_not_invoke_dispatcher() -> None:
    calls: list = []

    def _fake_dispatcher(reflex: dict) -> dict:
        calls.append(reflex)
        return {}

    review = build_delivery_mission_policy_review(
        delivery_mission_contract=_contract(),
        sanitized_telemetry=_telemetry(30.0),
        now=NOW,
        px4_reflex_dispatcher=_fake_dispatcher,
    )
    assert calls == []
    assert "dispatch" not in review.px4_recovery_reflex


def test_end_to_end_reflex_exhaustion_dispatches_real_rtl_over_loopback() -> None:
    """Full chain: policy review -> reflex -> real MAVLink RTL over loopback.

    Uses the actual dispatch_px4_recovery_reflex_rtl against a fake PX4
    endpoint on loopback, proving the wiring works end to end without a
    real SITL container.
    """

    import socket
    import threading

    from src.runtime.px4_gazebo_emergency_dispatcher import (
        MAV_CMD_NAV_RETURN_TO_LAUNCH,
    )
    from src.runtime.px4_mavlink_ack_state import (
        MAV_RESULT_ACCEPTED,
        encode_mavlink2_command_ack,
    )
    from src.runtime.px4_real_mavlink_transport import decode_mavlink2_frame
    from src.runtime.px4_recovery_reflex_dispatch import dispatch_px4_recovery_reflex_rtl

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.settimeout(5.0)
    _host, server_port = server_sock.getsockname()

    def _serve() -> None:
        remote = None
        while True:
            try:
                data, addr = server_sock.recvfrom(2048)
            except OSError:
                return
            remote = addr
            try:
                decoded = decode_mavlink2_frame(data)
            except Exception:
                continue
            if decoded["msg_id"] == 76:
                ack = encode_mavlink2_command_ack(
                    command_id=MAV_CMD_NAV_RETURN_TO_LAUNCH,
                    result_code=MAV_RESULT_ACCEPTED,
                )
                server_sock.sendto(ack, remote)
                return

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    def _real_dispatcher(reflex: dict) -> dict:
        result = dispatch_px4_recovery_reflex_rtl(
            reflex=reflex,
            live_mavlink_opt_in=True,
            endpoint_port=server_port,
            ack_timeout_seconds=3.0,
            heartbeat_warmup_frames=1,
            heartbeat_warmup_interval_seconds=0.0,
            now=NOW,
        )
        return result.model_dump(mode="json")

    try:
        review = build_delivery_mission_policy_review(
            delivery_mission_contract=_contract(),
            sanitized_telemetry=_telemetry(20.0),
            now=NOW,
            px4_reflex_dispatcher=_real_dispatcher,
        )
    finally:
        thread.join(timeout=2.0)
        server_sock.close()

    dispatch = review.px4_recovery_reflex["dispatch"]
    assert dispatch["dispatch_status"] == "dispatched"
    assert dispatch["reflex_authority_performed"] is True
    assert dispatch["operator_approval_performed"] is False
    assert dispatch["command_ack_observed"] is True
    assert dispatch["command_ack_accepted"] is True
    assert dispatch["physical_execution_invoked"] is False
