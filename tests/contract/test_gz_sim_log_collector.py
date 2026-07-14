from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.runtime.gz_sim_log_collector import (
    GzSimLogCollectorError,
    attach_gz_sim_delivery_world_hil_review_gate_artifacts,
    attach_gz_sim_log_hil_review_gate_artifacts,
    collect_gz_sim_delivery_world_log_sanitized,
    collect_gz_sim_log_sanitized,
)
from src.runtime.task_store import TaskStore


CAPTURED_AT = datetime(2026, 7, 15, tzinfo=timezone.utc)
EMPTY_WORLD_LOG = """\
[Msg] Gazebo Sim Server v8.9.0
[Msg] Loading SDF world file [/tmp/empty.sdf]
[Msg] Loaded level [default]
"""
DELIVERY_WORLD_LOG = EMPTY_WORLD_LOG.replace(
    "/tmp/empty.sdf",
    "/worlds/delivery_minimal.sdf",
)


@pytest.mark.parametrize(
    ("log_text", "collector", "attacher", "vehicle_id", "world_name"),
    [
        (
            EMPTY_WORLD_LOG,
            collect_gz_sim_log_sanitized,
            attach_gz_sim_log_hil_review_gate_artifacts,
            "gz-sim-harmonic-empty-world",
            "empty",
        ),
        (
            DELIVERY_WORLD_LOG,
            collect_gz_sim_delivery_world_log_sanitized,
            attach_gz_sim_delivery_world_hil_review_gate_artifacts,
            "gz-sim-delivery-world",
            "delivery_minimal",
        ),
    ],
)
def test_gz_sim_log_fixture_preserves_read_only_evidence_contract(
    tmp_path: Path,
    log_text,
    collector,
    attacher,
    vehicle_id: str,
    world_name: str,
) -> None:
    telemetry = collector(
        log_text,
        captured_at=CAPTURED_AT,
        provenance={"fixture_backed": True},
    )

    assert telemetry.source_kind == "gz_sim_harmonic_stdout_log"
    assert telemetry.vehicle_id == vehicle_id
    assert telemetry.measurements["gazebo_process_started"] is True
    assert telemetry.measurements["world_loaded"] is True
    assert telemetry.metadata["world_name"] == world_name
    assert telemetry.metadata["fixture_backed"] is True
    assert telemetry.read_only is True
    assert telemetry.command_payload_allowed is False
    assert telemetry.ros_dispatch_allowed is False
    assert telemetry.mavlink_dispatch_allowed is False
    assert telemetry.actuator_execution_allowed is False
    assert telemetry.live_execution_allowed is False
    assert telemetry.physical_execution_invoked is False

    store = TaskStore(str(tmp_path / "tasks.db"))
    task = store.create(
        kind="control_supervisor",
        title="Gazebo log collector fixture contract",
        status="running",
        artifacts={"existing": {"kept": True}},
    )
    artifacts = attacher(
        task["task_id"],
        log_text,
        captured_at=CAPTURED_AT,
        provenance={"fixture_backed": True},
        task_store_factory=lambda: store,
    )
    stored = store.get(task["task_id"])

    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"kept": True}
    assert "approval" not in stored["artifacts"]
    assert "promotion_package" not in stored["artifacts"]
    assert "runtime_reuse" not in stored["artifacts"]
    assert artifacts["autonomy_gate_result"]["passed"] is True
    assert artifacts["autonomy_gate_result"]["stronger_execution_allowed"] is False
    assert artifacts["autonomy_gate_result"]["live_execution_allowed"] is False
    assert artifacts["autonomy_gate_result"]["physical_execution_invoked"] is False


def test_gz_sim_log_fixture_rejects_command_like_payload() -> None:
    with pytest.raises(GzSimLogCollectorError, match="command-like fields"):
        collect_gz_sim_log_sanitized(
            EMPTY_WORLD_LOG + 'dispatch={"action":"takeoff"}\n',
            captured_at=CAPTURED_AT,
        )
