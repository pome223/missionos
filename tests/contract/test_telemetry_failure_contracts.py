import json
from pathlib import Path
from typing import Callable

import pytest

from src.runtime.gazebo_log_collector import (
    GazeboLogCollectorError,
    attach_gazebo_log_smoke_artifacts,
)
from src.runtime.gz_sim_log_collector import (
    GzSimLogCollectorError,
    attach_gz_sim_delivery_world_failure_diagnostics_artifact,
    attach_gz_sim_delivery_world_hil_review_gate_artifacts,
    attach_gz_sim_failure_diagnostics_artifact,
    attach_gz_sim_log_hil_review_gate_artifacts,
)
from src.runtime.px4_gazebo_log_collector import (
    Px4GazeboLogCollectorError,
    attach_px4_gazebo_log_smoke_artifacts,
)
from src.runtime.px4_sitl_log_collector import (
    Px4SitlLogCollectorError,
    attach_px4_sitl_log_hil_review_gate_artifacts,
)
from src.runtime.task_store import TaskStore
from tests.fixtures.telemetry_failure_cases import (
    CAPTURED_AT,
    GAZEBO_LOG_FAILURE_CASES,
    GZ_SIM_DELIVERY_FAILURE_CASES,
    GZ_SIM_FAILURE_CASES,
    PX4_GAZEBO_LOG_FAILURE_CASES,
    PX4_SITL_FAILURE_CASES,
    TelemetryFailureCase,
)


FORBIDDEN_ARTIFACTS = {
    "px4_gazebo_sanitized_telemetry",
    "hil_telemetry_envelope",
    "hil_telemetry_evidence",
    "hil_telemetry_review",
    "autonomy_gate_result",
    "approval",
    "promotion_package",
    "reuse_plan",
    "runtime_reuse",
}


def _new_task(store: TaskStore, case_id: str) -> dict:
    return store.create(
        kind="control_supervisor",
        title=f"Telemetry failure contract: {case_id}",
        status="running",
        artifacts={"existing": {"case_id": case_id, "kept": True}},
    )


def _assert_failure_preserved_task(
    store: TaskStore, task_id: str, case_id: str
) -> dict:
    stored = store.get(task_id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["artifacts"]["existing"] == {"case_id": case_id, "kept": True}
    assert FORBIDDEN_ARTIFACTS.isdisjoint(stored["artifacts"])
    return stored


PLAIN_COLLECTOR_CASES: tuple[
    tuple[
        str,
        tuple[TelemetryFailureCase, ...],
        type[Exception],
        Callable[..., dict],
    ],
    ...,
] = (
    (
        "px4_sitl",
        PX4_SITL_FAILURE_CASES,
        Px4SitlLogCollectorError,
        lambda task_id, log_text, store: attach_px4_sitl_log_hil_review_gate_artifacts(
            task_id,
            log_text,
            captured_at=CAPTURED_AT,
            task_store_factory=lambda: store,
        ),
    ),
    (
        "px4_gazebo_log",
        PX4_GAZEBO_LOG_FAILURE_CASES,
        Px4GazeboLogCollectorError,
        lambda task_id, log_text, store: attach_px4_gazebo_log_smoke_artifacts(
            task_id, log_text, task_store_factory=lambda: store
        ),
    ),
    (
        "gazebo_log",
        GAZEBO_LOG_FAILURE_CASES,
        GazeboLogCollectorError,
        lambda task_id, log_text, store: attach_gazebo_log_smoke_artifacts(
            task_id, log_text, task_store_factory=lambda: store
        ),
    ),
)


@pytest.mark.parametrize(
    ("collector_id", "cases", "error_type", "attacher"),
    PLAIN_COLLECTOR_CASES,
    ids=[case[0] for case in PLAIN_COLLECTOR_CASES],
)
def test_invalid_logs_fail_closed_without_mutating_task(
    tmp_path: Path,
    collector_id: str,
    cases: tuple[TelemetryFailureCase, ...],
    error_type: type[Exception],
    attacher: Callable[..., dict],
) -> None:
    store = TaskStore(str(tmp_path / f"{collector_id}.db"))

    for case in cases:
        task = _new_task(store, case.case_id)
        with pytest.raises(error_type):
            attacher(task["task_id"], case.log_text, store)
        _assert_failure_preserved_task(store, task["task_id"], case.case_id)


GZ_SIM_COLLECTOR_CASES = (
    (
        "empty_world",
        GZ_SIM_FAILURE_CASES,
        attach_gz_sim_log_hil_review_gate_artifacts,
        attach_gz_sim_failure_diagnostics_artifact,
    ),
    (
        "delivery_world",
        GZ_SIM_DELIVERY_FAILURE_CASES,
        attach_gz_sim_delivery_world_hil_review_gate_artifacts,
        attach_gz_sim_delivery_world_failure_diagnostics_artifact,
    ),
)


@pytest.mark.parametrize(
    ("collector_id", "cases", "attacher", "diagnostics_attacher"),
    GZ_SIM_COLLECTOR_CASES,
    ids=[case[0] for case in GZ_SIM_COLLECTOR_CASES],
)
def test_invalid_gz_sim_logs_create_debug_diagnostics_only(
    tmp_path: Path,
    collector_id: str,
    cases: tuple[TelemetryFailureCase, ...],
    attacher: Callable[..., dict],
    diagnostics_attacher: Callable[..., dict],
) -> None:
    store = TaskStore(str(tmp_path / f"{collector_id}.db"))

    for case in cases:
        task = _new_task(store, case.case_id)
        with pytest.raises(GzSimLogCollectorError) as captured:
            attacher(task["task_id"], case.log_text, task_store_factory=lambda: store)
        diagnostics_attacher(
            task["task_id"],
            case.log_text,
            error_message=str(captured.value),
            provenance={"case_id": case.case_id},
            task_store_factory=lambda: store,
        )
        stored = _assert_failure_preserved_task(
            store, task["task_id"], case.case_id
        )
        diagnostics = stored["artifacts"]["gz_sim_telemetry_diagnostics"]

        assert diagnostics["schema_version"] == "gz_sim_telemetry_diagnostics.v1"
        assert diagnostics["status"] == "invalid_evidence"
        assert diagnostics["metadata"]["debug_only"] is True
        assert diagnostics["metadata"]["case_id"] == case.case_id
        assert diagnostics["hil_artifacts_persisted"] is False
        assert diagnostics["gate_artifacts_persisted"] is False
        assert diagnostics["approval_promotion_reuse_created"] is False
        if case.case_id == "command_like_payload":
            serialized = json.dumps(diagnostics, ensure_ascii=True, sort_keys=True)
            assert "/cmd_vel" not in serialized
            assert '"nested"' not in serialized
