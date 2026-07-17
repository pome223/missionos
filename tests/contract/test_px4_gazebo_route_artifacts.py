from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import artifacts


FIXED_TIME = datetime(2026, 7, 15, 12, 34, 56, tzinfo=timezone.utc)


def test_legacy_entrypoint_delegates_artifact_io_to_package() -> None:
    assert route_entrypoint._new_run_dir is artifacts.create_run_directory
    assert route_entrypoint._write_run_artifacts is artifacts.write_run_artifacts
    assert route_entrypoint._write_recovery_run_artifacts is artifacts.write_recovery_run_artifacts
    assert route_entrypoint._mark_cleanup_observed is artifacts.mark_cleanup_observed
    assert (
        route_entrypoint._snapshot_task_database_evidence
        is artifacts.snapshot_task_database_evidence
    )


def test_artifact_root_reads_only_explicit_environment_mapping(tmp_path: Path) -> None:
    assert artifacts.artifact_root(environ={}) == Path(artifacts.DEFAULT_ARTIFACT_ROOT)
    assert artifacts.artifact_root(environ={artifacts.ARTIFACT_ROOT_ENV: str(tmp_path)}) == tmp_path


def test_run_directory_uses_timestamp_and_collision_suffix(tmp_path: Path) -> None:
    first = artifacts.create_run_directory(root=tmp_path, observed_at=FIXED_TIME)
    second = artifacts.create_run_directory(root=tmp_path, observed_at=FIXED_TIME)

    assert first.name == "horizontal_route_20260715T123456Z"
    assert second.name == "horizontal_route_20260715T123456Z_2"
    assert first.is_dir()
    assert second.is_dir()


def test_json_and_jsonl_serialization_are_stable(tmp_path: Path) -> None:
    json_path = tmp_path / "summary.json"
    jsonl_path = tmp_path / "samples.jsonl"

    artifacts.write_json(json_path, {"z": 2, "a": 1})
    artifacts.write_jsonl(jsonl_path, [{"z": 2}, {"a": 1}])

    assert json_path.read_text() == '{\n  "a": 1,\n  "z": 2\n}\n'
    assert jsonl_path.read_text() == '{"z": 2}\n{"a": 1}\n'


def test_recovery_run_artifacts_preserve_supplied_claim_boundaries(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    task_db_path = tmp_path / "source-tasks.db"
    task_db_path.write_bytes(b"task-store-evidence")

    artifacts.write_recovery_run_artifacts(
        run_dir=run_dir,
        summary={
            "final_status": "blocked",
            "delivery_completion_claimed": False,
            "physical_execution_invoked": False,
        },
        task_artifacts={"existing": {"kept": True}},
        pose_rows=[{"phase": "recovery", "observed": True}],
        log_text="bounded recovery log\n",
        task_db_path=task_db_path,
        recorded_at=FIXED_TIME,
    )

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["final_status"] == "blocked"
    assert summary["delivery_completion_claimed"] is False
    assert summary["physical_execution_invoked"] is False
    mission_artifacts = json.loads((run_dir / "mission_artifacts.json").read_text())
    assert mission_artifacts == {
        "recorded_at": FIXED_TIME.isoformat(),
        "frozen_for_test": False,
        "artifacts": {"existing": {"kept": True}},
    }
    assert (run_dir / "pose_samples.jsonl").read_text() == (
        '{"observed": true, "phase": "recovery"}\n'
    )
    assert (run_dir / "px4_docker.log").read_text() == "bounded recovery log\n"
    assert (run_dir / "tasks.db").read_bytes() == b"task-store-evidence"


def test_route_artifact_writer_preserves_existing_live_trace(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    trace_path = run_dir / "pose_samples.jsonl"
    trace_path.write_text('{"phase":"live"}\n')

    artifacts.write_run_artifacts(
        run_dir=run_dir,
        summary={
            "final_status": "completed",
            "delivery_completion_claimed": True,
            "physical_execution_invoked": False,
        },
        task_artifacts={"verifier": {"observed": True}},
        pose_rows=None,
        log_text="normal route log\n",
        task_db_path=None,
        recorded_at=FIXED_TIME,
    )

    assert trace_path.read_text() == '{"phase":"live"}\n'
    assert json.loads((run_dir / "summary.json").read_text()) == {
        "delivery_completion_claimed": True,
        "final_status": "completed",
        "physical_execution_invoked": False,
    }
    mission_artifacts = json.loads((run_dir / "mission_artifacts.json").read_text())
    assert mission_artifacts["artifacts"] == {"verifier": {"observed": True}}
    assert not (run_dir / "tasks.db").exists()


def test_task_database_snapshot_survives_temporary_store_lifetime(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with TemporaryDirectory(dir=tmp_path) as temporary_store_dir:
        task_db_path = Path(temporary_store_dir) / "tasks.db"
        task_db_path.write_bytes(b"live-task-store-evidence")
        snapshot_path = artifacts.snapshot_task_database_evidence(
            task_db_path=task_db_path,
            run_dir=run_dir,
        )
        assert task_db_path.exists()

    assert not task_db_path.exists()
    assert snapshot_path == run_dir / "tasks.db"
    assert snapshot_path.read_bytes() == b"live-task-store-evidence"

    artifacts.write_run_artifacts(
        run_dir=run_dir,
        summary={"final_status": "completed"},
        task_artifacts={"existing": {"kept": True}},
        pose_rows=[],
        log_text="completed after TaskStore shutdown\n",
        task_db_path=None,
        recorded_at=FIXED_TIME,
    )
    assert snapshot_path.read_bytes() == b"live-task-store-evidence"


def test_cleanup_receipt_update_preserves_claim_boundaries(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    artifacts.write_json(
        summary_path,
        {
            "scenario_cleanup_receipt": {"cleanup_status": "teardown_required"},
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
    )

    assert artifacts.mark_cleanup_observed(tmp_path, observed_at=FIXED_TIME) is True
    summary = json.loads(summary_path.read_text())
    assert (
        summary["scenario_cleanup_receipt"]["cleanup_status"]
        == "isolated_container_teardown_observed"
    )
    assert summary["scenario_cleanup_receipt"]["observed_at"] == FIXED_TIME.isoformat()
    assert summary["dispatch_authority_created"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["delivery_completion_claimed"] is False


def test_cleanup_receipt_update_is_noop_without_receipt(tmp_path: Path) -> None:
    assert artifacts.mark_cleanup_observed(tmp_path, observed_at=FIXED_TIME) is False
    artifacts.write_json(tmp_path / "summary.json", {"final_status": "blocked"})
    assert artifacts.mark_cleanup_observed(tmp_path, observed_at=FIXED_TIME) is False
